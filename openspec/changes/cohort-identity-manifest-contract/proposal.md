# Proposal — cohort-identity-manifest-contract (#2542, #2557, #2546, #2555, #2539)

## Why

Five file-journal (db-free) scheduler/chain defects share one theme: the identity a cohort row or a run manifest carries does not match what its consumers assume.

- **#2542 mixed-floor cohort retry.** A cohort retry charges every member the master's attempt (`max(floors)+1`). A low-floor member is billed for retries it never consumed. In a ~30-model cohort, one member near the limit exhausts the budget for everyone and forces an operator budget re-entry. The behavior fails closed.
- **#2557 stale `hydro_run.submission_attempt`.** On a same-`run_id` rerun, `create_hydro_run_from_basin` keeps a non-retriable existing row unchanged. That row's `submission_attempt` stays at the old value, while the reservation and runtime manifest carry the new attempt. The three accepted-submit release entrypoints match on `hydro.submission_attempt == master attempt`, so they silently skip the stale row, which then stays `active`.
- **#2546 convert rows record no members.** PR #2656 B4 made the parse/state_save_qc/publish model-less cohort rows record `cohort_members`. Convert still does not. While the default cohort's convert row is active, the override cohort's conflict check (`has_active_pipeline`) matches that row, raises `PIPELINE_ALREADY_ACTIVE`, and records `submission_failed` for the pass.
- **#2555 None-lane manifest-missing precedes §8.7.** A `terminal_*_success` row can combine a missing run-manifest initial state with a stale journal lineage. The `terminal_run_manifest_missing` branch takes such a row before `_journal_predecessor_identity_quarantine` ever sees it. The row then reruns from forecast with no quarantine provenance, and the §8.7 breaker never counts it. `retry_terminal_run_manifest_missing` has no retry budget of its own (verified), so a manifest that stays missing loops without bound.
- **#2539 runtime manifest vs schema.** `build_forecast_runtime_manifest` emits 9 top-level keys that `run_manifest.schema.json` does not declare. It also lacks the required top-level `schema_version` and `runtime.executable`. Nothing validates real builder output against the schema.

## What changes

- **#2542:**
  - The cohort master records a per-member floor field `retry_attempt_floors` at reservation.
  - Reconcile charges each member its own base plus the extra attempts the master consumed: `own_floor+1` for a floored member, `0` for an unlisted one, capped at the shared charge. An empty or absent floor list keeps the shared charge.
  - The master id is still minted above `max(floors)`.
  - A master row without the field keeps the shared charge.
- **#2557:**
  - First reproduce the defect through the real write path.
  - If it reproduces, a same-`run_id` rerun raises the existing row's `submission_attempt` monotonically to the reservation attempt. The row's status is not changed.
  - If it does not reproduce, record why and do not change code.
- **#2546:**
  - B4 member recording extends to the model-less cohort `convert` row.
  - `has_active_pipeline` then excludes a sibling cohort's convert row by the existing rule.
- **#2555:**
  - On the None lane, a manifest-missing terminal skip first goes through the journal-predecessor identity quarantine. A positive quarantine decision (retry or breaker-blocked) takes over.
  - Otherwise the existing `retry_terminal_run_manifest_missing` is emitted unchanged.
  - Both still consult the forcing witness.
- **#2539:**
  - The schema declares the 9 keys.
  - The builder writes top-level `schema_version`.
  - `runtime.executable` becomes optional in the schema, because the SHUD runtime takes the executable from `SHUD_EXECUTABLE`, not from the manifest.
  - Builder-nullable fields (cold start / packaged IC) are declared nullable.
  - Regression tests validate the real builder output (warm, cold, packaged IC) against the schema.
  - `validate_forecast_runtime_manifest` keeps its behavior, and every key it requires becomes schema-declared. There is no runtime schema check, so no manifest accepted today is rejected.

## Out of scope

- The DB lane, for all five issues.
- Single-model mint and charging (#2404).
- The strict lane's terminal-skip dispatch (#2555).
- Analysis and cycle-stage manifests (#2539). Any violations found there are recorded only.
- Extracting `chain_forecast_orchestrator_cycle.py` below 1000 lines. `.large-file-guard.json` already excludes the file (PR #2584), so the extraction prerequisite in #2542 is stale. `chain_manifests.py` and `accepted_submit_identity.py` are not excluded; they are added to the exclude list with a rationale (task 0.1).
- `build_forecast_run_manifest`, the trigger path (#2539). Its violations are recorded only.
- #2655.
