## Context

Fixture level: expanded

Repair intensity: high

Project profile: NHMS

Issue #383 is a production automation bug in the IFS provider boundary. `IFSAdapter._url_exists()` currently converts any `IFSAdapterError` other than `ForbiddenSourceError` into `False`. During node-22 production, DNS failures across AWS, Azure, Google, and ECMWF mirrors therefore flowed through `discover_cycles()` as `status=unavailable` / `classifier=unavailable`, even though the source cycle later proved available. This crosses an external-provider discovery surface, CLI output, scheduler evidence, and retry semantics.

## Goals / Non-Goals

Goals:
- Preserve the distinction between source object absence and compute-node network/probe failure.
- Return the stable retryable classification `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, and `retryable=true` for DNS/network/timeout probe failures.
- Include redacted attempted mirror evidence and concrete error class/message in adapter/CLI/scheduler evidence.
- Preserve current behavior for genuine 404/unpublished cycles, forbidden sources, and successful discovery.

Non-Goals:
- No new database status enum or schema migration.
- No change to mirror ordering, IFS variable policy, canonical conversion, forcing, or frontend behavior.
- No automatic repair of already-stale historical DB evidence from the 2026-06-09 incident; that is covered by later retry/evidence issues.
- No Slurm gateway contract change.

## Decisions

1. Use a typed probe-failure result instead of treating failed probes as `False`.
   - Rationale: `False` already means "not found or not published" for discovery; network failures need evidence, retryability, and a different classifier.
   - Alternative rejected: raise all network errors out of `discover_cycles()`. That would lose per-cycle evidence and make CLI behavior less useful for scheduler automation.

2. Keep 404/unpublished as `status=unavailable` with `reason=source_cycle_unavailable`.
   - Rationale: existing scheduler/runbook semantics rely on unavailable source-cycle latency being non-fatal but visible.

3. Attach attempted mirror details with redacted URI/source/error fields.
   - Rationale: operators need to distinguish "every mirror DNS failed" from "one mirror 404 then another succeeded" without leaking credentials or signed URLs.

4. Treat network/probe failures as retryable scheduler evidence.
   - Rationale: compute-node DNS or transient connectivity failures should remain retryable and must not be mistaken for a terminal manual-only source absence.

## Risk Packs Considered

- Public API / CLI / script entry: selected - `nhms-ifs download --cycle-time` emits operator-facing payloads.
- Config / project setup: selected - behavior depends on configured IFS mirrors and node-22 network boundary.
- File IO / path safety / overwrite: not selected - no file path, overwrite, or object-store write semantics change.
- Schema / columns / units / field names: selected - evidence payload fields and classifiers must remain stable.
- Auth / permissions / secrets: selected - probe evidence must be redacted and must not leak credentials or signed URLs.
- Concurrency / shared state / ordering: not selected - no concurrent state transitions are changed by this PR.
- Resource limits / large input / discovery: selected - the bug is in external cycle discovery across mirrors.
- Legacy compatibility / examples: selected - existing unavailable/forbidden/discovered statuses must remain compatible.
- Error handling / rollback / partial outputs: selected - network failures must be retryable and non-misleading.
- Release / packaging / dependency compatibility: not selected - no dependency or packaging change.
- Documentation / migration notes: selected - node-22 operator runbook must explain recovery.

Domain packs:
- External hydro-met providers / snapshot reproducibility: selected - IFS mirror probe results must bind to the same source cycle.
- Slurm production lifecycle / mock-vs-real parity: selected - Slurm download evidence and scheduler retry behavior consume the CLI result.
- Run manifest / QC provenance: selected - attempted source and error evidence must be audit-safe.
- Hydro-met time series / forcing windows: not selected - no forcing window or time-series semantics change.
- Geospatial / CRS / basin geometry: not selected - no geometry surface touched.
- SHUD numerical runtime / conservation / NaN: not selected - no runtime/numerical surface touched.
- PostGIS / TimescaleDB domain behavior: not selected - no DB schema/query semantic change.
- Published NHMS artifacts / display identity: not selected - no publish/display artifact change.

## Invariant Matrix

Governing invariant: IFS source-cycle discovery MUST report provider object absence and compute-node network/probe failure as distinct, retryable evidence states across adapter, CLI, scheduler evidence, and runbook guidance.

Source-of-truth identity/contract: `source_id=IFS`, `cycle_time`, probe forecast hour `0`, configured mirror/source list, `status`, `reason`, `classifier`, `retryable`, redacted `attempted_sources` evidence.

Surfaces:
- Producers: `workers/data_adapters/ifs_adapter.py::_discover_cycle_availability`, `_url_exists`, and related probe helpers.
- Validators/preflight: adapter tests and CLI argument handling in `workers/data_adapters/cli.py`.
- Storage/cache/query: no schema change; forecast-cycle upsert must still occur only for discovered cycles.
- Public routes/entrypoints: `nhms-ifs download --cycle-time ...` CLI payload.
- Frontend/downstream consumers: scheduler/readiness evidence consumers under `services/orchestrator`; no frontend change.
- Failure paths/rollback/stale state: all mirrors DNS/network failing; mixed 404 and network failures; forbidden source.
- Evidence/audit/readiness: `CycleDiscovery.as_dict()`, CLI JSON payload, scheduler source-cycle evidence, runbook notes.

Regression rows:
- Successful mirror probe for `IFS 2026060800 f000` -> `available=true`, `status=discovered`, existing forecast-cycle upsert behavior preserved.
- All mirrors return 404/unpublished -> `available=false`, `status=unavailable`, `reason=source_cycle_unavailable`, `classifier=unavailable`, no forecast-cycle upsert.
- All mirrors raise DNS/network/timeout errors -> `available=false`, `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, `retryable=true`, attempted mirror evidence includes redacted error class/message.
- Mixed provider 404 plus network failures with no successful mirror -> network/probe failure is not collapsed to source unavailable when network failure prevents reliable source-cycle determination.
- Forbidden source probe -> existing `status=forbidden`, `retryable=false` behavior preserved.
- CLI consumer of probe failure -> JSON contains `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, `retryable=true`, `files=0`, `total_bytes_written=0`, and redacted attempted mirrors.
- Scheduler consumer of probe failure -> evidence remains retryable and carries attempted mirrors/error cause without secrets, and genuine unavailable cycles still use the existing source-latency path.

## Boundary-Surface Checklist

- Shared helper roots: IFS adapter probe and safe-text redaction helpers.
- Public entrypoints: `nhms-ifs download`.
- Read surfaces: discovery evidence consumed by scheduler.
- Write/delete/overwrite surfaces: none.
- Staging/publish/rollback surfaces: none in this issue; stale evidence supersession is issue #386.
- Producer/consumer evidence boundaries: adapter `CycleDiscovery`, CLI JSON, scheduler source-cycle evidence.
- Stale-state/idempotency boundaries: forecast-cycle upsert must not mark network probe failures as discovered or raw complete.
- Unchanged downstream consumers: GFS adapter, canonical conversion, forcing, SHUD runtime, frontend.

## Risks / Trade-offs

- Risk: changing status strings can break tests or consumers expecting only `discovered|unavailable|forbidden`. Mitigation: add focused scheduler/CLI tests and preserve existing strings for genuine source absence.
- Risk: evidence could leak sensitive URL content. Mitigation: use existing redaction helpers for URIs/messages and assert no credentials in regression evidence.
- Risk: over-classifying mixed 404/network cases as network failure may hide true source latency. Mitigation: only use probe-failure classification when network errors prevent reliable all-mirror absence determination; keep all-404 as unavailable.
