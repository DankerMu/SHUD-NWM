## Context

`skipped_duplicate_submission` is the dedicated non-success terminal for a reserve-gate deferral. The readiness vocabulary already treats it as review-visible blocked state, while the production status translator falls through to its unknown-status `failed` default. Both stage projection functions consume that translator, so one alias is the narrow single-source fix.

- Fixture level: expanded
- Project profile: NHMS
- Repair intensity: medium
- Upstream suggested level: absent

## Goals / Non-Goals

**Goals:**

- Render duplicate-submission deferral as `blocked` on every production stage-evidence path.
- Keep the raw stage status unchanged and preserve unknown-status fail-closed behavior.
- Prove the two projection consumers follow the common translator.

**Non-Goals:**

- Change reserve-gate, retry, stage-terminal, or readiness behavior.
- Add a production status taxonomy member.
- Reclassify generic `skip` / `skipped` aliases.
- Change any unknown-status fallback.

## Decisions

1. Add only `"skipped_duplicate_submission": "blocked"` to the existing alias table. `blocked` already means work prevented by an external condition and matches the readiness plane; `superseded` would imply replacement rather than temporary reservation contention.
2. Keep `production_status_for` as the sole translator. Neither projection consumer gets a local exception or a second status list.
3. Cover the private projection helpers through existing module tests because they are the actual evidence-construction seams named by the issue.

## Risk Packs Considered

- Public API / CLI / script entry: not selected — no public entrypoint changes.
- Config / project setup: not selected — no configuration changes.
- File IO / path safety / overwrite: not selected — no file operations.
- Schema / columns / units / field names: selected — production evidence status vocabulary changes.
- Auth / permissions / secrets: not selected — no security boundary.
- Concurrency / shared state / ordering: selected — preserve reserve-gate deferral semantics while changing only its evidence translation.
- Resource limits / large input / discovery: not selected — constant-size mapping.
- Legacy compatibility / examples: selected — preserve generic skip aliases and unknown-to-failed fallback.
- Error handling / rollback / partial outputs: selected — fail-closed unknown status must remain failed.
- Release / packaging / dependency compatibility: not selected — no dependency or packaging change.
- Documentation / migration notes: not selected — OpenSpec is the contract record; no operator migration.
- Geospatial / CRS / basin geometry: not selected — unrelated.
- Hydro-met time series / forcing windows: not selected — unrelated.
- SHUD numerical runtime / conservation / NaN: not selected — unrelated.
- PostGIS / TimescaleDB domain behavior: not selected — no DB state.
- Slurm production lifecycle / mock-vs-real parity: not selected — submission behavior is unchanged.
- External hydro-met providers / snapshot reproducibility: not selected — unrelated.
- Run manifest / QC provenance: selected — both production evidence projections must carry the corrected status.
- Published NHMS artifacts / display identity: not selected — no artifact identity change.

## Invariant Matrix

- Governing invariant: A duplicate-submission reserve deferral remains raw `skipped_duplicate_submission` and projects as `blocked` everywhere, while unrecognized statuses still project as `failed`.
- Source-of-truth identity/contract: raw stage `status` plus `PRODUCTION_STATUS_TAXONOMY` and `production_status_for`.
- Producers: `chain_forecast_submission.py` emits the dedicated status; unchanged.
- Validators/preflight: `production_status_for` is the sole translator.
- Storage/cache/query: none — no persistence change.
- Public routes/entrypoints: none — evidence construction only.
- Frontend/downstream consumers: `_candidate_stage_evidence_item`, `_stage_run_evidence`, and readiness status vocabulary.
- Failure paths/rollback/stale state: unknown token remains `failed`; reverting the alias restores prior conservative behavior.
- Evidence/audit/readiness: production scheduler tests exercise direct and both projected forms; readiness remains unchanged.
- Regression rows:
  - dedicated duplicate status -> `blocked` in direct and both stage projections.
  - unknown status -> `failed` in direct and both applicable translation paths.
  - generic `skip` / `skipped` -> existing `superseded` compatibility behavior.

## Boundary Surface Checklist

- Shared helper root: `production_status_for` changed once.
- Producer: inspected, intentionally unchanged.
- Evidence consumers: both projection helpers covered.
- Readiness consumer: inspected, intentionally unchanged because it already classifies blocked.
- Unchanged sibling statuses: generic skip and unknown fallback covered.

## Risks / Trade-offs

- A local projection exception could drift from the translator -> tests call both consumers and require the shared result.
- Over-broad alias changes could weaken fail-closed evidence -> negative unknown-status oracle remains mandatory.
- No migration is required; the change affects newly generated evidence only. Revert is the containment path.
