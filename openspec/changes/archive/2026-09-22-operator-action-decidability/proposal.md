## Why

`list-operator-actions` is the operator's only db-free enumeration of pending manual actions on node-22. Its `exit 0` means "nothing waits, and the window is trustworthy". Seven open issues (batch D, one PR at the user's request) either produce a false `exit 0`, make a healthy pass look blocked, destroy evidence the reader needs, or leave the surface untested:

- #1905 — a candidate-heavy pass over `MAX_EVIDENCE_BYTES` jumps straight from `_compact_admissible_pass_payload` to the bounded fallback, which rewrites the top-level status to `resource_limit_blocked` although the pass submitted. The reader then treats it as a size-fallback product (non-evaluating).
- #2402 — the bounded fallback clears `source_cycles` unconditionally with no `limit` marker, destroying the only db-free record of §8.7 breaker-released cycles.
- #2443 — `backfill.enabled` records config intent, not the leg `discover_cycles` executed. The read side's false `exit 0` was already closed on this tree (`no_models_evaluated` from `counts.selected_model_count == 0`); what remains is the write side: evidence cannot say which leg ran.
- #2442 — `EVALUATING_PASS_STATUSES` is a hand-copied 24-literal allowlist pinned only by a self-copy test; no mechanical binding to the writers.
- #2405 — a submit pass that crashes after writing `<pass>.pre_execution.json` but before its terminal file is invisible to the reader (suffix excluded), so an older pass keeps answering `exit 0`; the reservation carries no liveness, so in-flight and crashed cannot be told apart.
- #2399 — the runbook's first step dereferences a bare `$NHMS_SCHEDULER_EVIDENCE_ROOT`; node-22 `compute.env` still points at the empty 22-e2e root while its header claims alignment → a scan of an empty dir answers `exit 0`.
- #2426 — `cycle_time_invalid` has zero tests; `decision_not_reentry_eligible` and the `type(pin) is not int` half are unreachable from both CLI entrypoints, and the runbook promises a receipt the operator can never see.

## What Changes

- #1905: new `non_blocking_summary` tier between `_compact_admissible_pass_payload` and the bounded fallback: candidate lists (incl. non-terminal skipped rows) become the existing bounded candidate summary rows (which retain every field the reader consumes); all other top-level fields (status, `source_cycles`, counts, scope keys, backfill, `model_run_evidence`) are kept verbatim; `evidence_compaction` records the tier and nests the admission record. Still over → unchanged bounded fallback.
- #2402: the bounded fallback keeps a bounded projection of operator-relevant not-selected `source_cycles` entries (`selection_reason == journal_predecessor_identity_quarantine_breaker_engaged`) and marks `limit.source_cycles = summarized`; a fit tier that clears a non-empty list marks `dropped` (never downgraded). Reader: lists breaker-released cycles from the projection; the pass stays non-evaluating; an absent marker on a size-fallback product is read as `dropped`.
- #2443: `discover_cycles` emits a typed `backfill_leg` evidence entry; runtime writes `backfill = {enabled: <config>, mode: backfill|legacy, ...}`. Reader keeps `no_models_evaluated` for the zero-model case and adds a consistency guard (legacy mode with models selected → `scope_unknown`).
- #2405: reservation lease (design D4, option L2): the reservation embeds `lease = {ttl_seconds, heartbeat_interval_seconds}` and the existing scheduler lease heartbeat refreshes the reservation's mtime; the reader treats a reservation with no terminal artifact, newer (by `reserved_at`) than the newest evaluating pass, and stale (mtime age > 2×ttl) — or with no lease block — as an orphan that vetoes `exit 0` (exit 3, listed). A fresh one is in flight and changes nothing.
- #2442: AST closure pin binding `EVALUATING_PASS_STATUSES`/`TRANSPARENT_PASS_STATUSES` to the writer literals; the passthrough branch is a declared dynamic source; the asymmetry is documented; the self-copy assertion is replaced.
- #2399: runbook step 1 reads the root from `compute.scheduler-dbfree.env` (the unit's `EnvironmentFile`) and requires `passes_scanned > 0` and the expected `evidence_root`; `compute.example` placeholder fixed; node-22 `compute.env` scheduler-root keys aligned (ops action with backup and receipt) if nothing live sources it.
- #2426: remove the unreachable `decision_not_reentry_eligible` branch and the `type(pin) is not int` half (YAGNI: zero programmatic callers, `choices`/`type=int` same-source); add the `cycle_time_invalid` test leg; runbook reason list matches reachable receipts.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `production-scheduler-orchestration`: listing decidability — orphan reservations, zero-model backfill passes, summarized breaker-released cycles, writer-bound status allowlist.
- `runtime-evidence-and-operations`: non-blocking summary tier before the fail-closed fallback; `source_cycles` bounded projection and marker.

## Impact

- Code: `services/orchestrator/scheduler_evidence_payload.py`, `scheduler_evidence.py`, `scheduler_runtime.py`, `scheduler_discovery.py`, `scheduler_lease.py` (heartbeat touch), `operator_action_listing.py`, `operator_reentry_confirmation.py`, `scripts/node22_scheduler_evidence_retention.py` (orphan logging only if needed).
- Tests: new `tests/test_scheduler_evidence_decidability.py` (≤1000 lines); #2442 pin in a new `tests/test_operator_action_status_closure.py`; `tests/test_operator_reentry_confirmation.py` leg; self-copy removal in `tests/test_operator_action_listing.py`.
- Docs: `docs/runbooks/node22-control-plane-manual-recovery.md` (one coherent exit-code edit), `infra/env/compute.example`.
- Runtime: the db-free scheduler control plane on node-22 (compute/Slurm/artifact producer only; active DB writes and ingest validation stay on node-27) and the operator read surface. Evidence format gains optional keys only (older readers ignore them).

## Triage

```text
Issue type: bugfix x4 + test x2 + ops/docs x1 (one PR at user request)
Fixture level: expanded
Upstream suggested level: #1905 "focused" (not this project's vocabulary; expanded because persisted evidence format, CLI exit codes, file IO and legacy compat all trigger)
Blast radius: operator's only pending-action enumeration — a wrong change yields a false exit 0 (pending manual action unseen) or permanent exit 3 noise; writer changes alter persisted pass evidence on node-22 production.
Selected risk packs: public CLI; file IO; schema/field names; legacy compatibility; error handling; concurrency (lease heartbeat); documentation
Evidence floor: red-then-green per issue; the full-vs-summarized listing equivalence table; uv run ruff check .; node-27 pytest on the scheduler/listing suites at PR head
```
