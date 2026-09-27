# Design — retry-blocked-reads-failed-stage-restart

Fixture level: **expanded**. Scope is the retry lane's evidence and submission authority (which runtime roots a real Slurm submission uses) and the scheduler restart point (which stages are resubmitted).

## #2566 companion read

- `_file_retry_runtime_root_candidates` (`file_orchestration_journal.py` ~12077-12114) reads `query_pipeline_jobs_by_run(run_id)`.
- Before the `job_type` filter, scan the result with `_is_blocked_query_job`. On a hit:
  - log `LOGGER.warning` with `_blocked_query_job_fault`'s `reason`/`field`, in the same shape as the sibling `_file_retry_event_runtime_root_candidates` degrade;
  - contribute no companion candidate;
  - add 1 to the batch's `blocked_reads`.
- A real empty or companion-less run is unchanged: `blocked_reads` stays 0.
- **Re-verify.** Check the PR #2565 spec claim that the retry-id allocator `_next_file_manual_retry_job_id_for_run` is unreachable for a blocked read because the selector refuses first. It sits before the walk, in the locked `_create_pending_manual_retry_job`. Record the result as a test or comment. Do not change the allocator.

## #2567 blocked-read accounting and environment exclusion

- **Counter plumbing.** `_file_retry_previous_job_id` returns `str | None` today. Carry a blocked flag or count out of it, and out of `_file_retry_provenance_job_ids`, the event reader and the companion read (1.1): as a tuple, or a small result object. The implementer chooses and reports. A single blocked id can be counted twice (predecessor lookup + event scan), so tests assert `>= 1`.
- **Evidence key.** `_runtime_root_resolution_evidence` emits `candidate_counts.blocked_reads` **only when > 0**. Evidence without a blocked read is then byte-identical. That covers the file-lane golden `_FILE_LANE_LOCAL_ROOT_EVENT_DETAILS` (`tests/test_retry.py` ~3789/3866) and the whole DB lane, which never blocks. `_RuntimeRootCandidateBatch.blocked_reads: int = 0`.
- **Roots required.** When the accumulated `blocked_reads > 0`:
  - `_resolve_file_retry_runtime_roots` treats runtime roots as **required**, whatever the job type (download or not) and whatever `db_free_required`. Otherwise a non-download, non-db-free job (e.g. `run_shud_forecast_array`) returns `None` and submits without a root contract.
  - The `runtime_config:environment` candidate is not appended.
  - Resolution then either returns recorded-candidate roots or raises `RETRY_RUNTIME_ROOTS_UNRESOLVED` (`_RetryRuntimeRootResolutionError`, which carries `.code` and `.details.runtime_root_resolution`, `retry.py` ~418-424). That error goes through the governed `submission_failed` row + API 503 via `_retry_submission_error_code`/`_runtime_root_resolution_from_error`. It is not the #2387 fake `SBATCH_SUBMISSION_FAILED`.
  - Because roots are then always returned or raised, the `runtime_root_fields is None` → `os.getenv("WORKSPACE_ROOT")` fallback in `_file_manual_retry_array_tasks` (~856) is unreachable under a blocked read. Assert it in a test: no env root is read or used.
- **`db_free_required` (fixture review round 2).** When provenance is blocked, `db_free_required` still includes the environment's **policy switch** `NHMS_SCHEDULER_DB_FREE_REQUIRED`. It is a mode flag, not a root value, and production sets it (`infra/env/compute.example:65`). The db-free **selector values** come only from recorded candidates. A recorded candidate with complete roots but no selector is therefore not `db_free_complete`, and resolution raises `RETRY_RUNTIME_ROOTS_UNRESOLVED`. Only root/selector values are banned from the environment, not the policy switch.
- **Why refuse, not degrade.**
  - The environment root is current config, not the job's provenance. A blocked read means provenance is unknown, not absent.
  - The task requires that a rejection never silently falls back to env-root submission and is distinguishable from "no event".
  - Trade-off, stated in the PR: once a blocked read occurs, every job type fails with `RETRY_RUNTIME_ROOTS_UNRESOLVED` unless recorded provenance resolves.
  - PR #2565's "degrade may fall back to the environment" note is superseded.
- **Flipped test.** `tests/test_file_journal_read_blocked_consumers.py` ~779 asserts 200 + gateway requests for a blocked provenance read. It is intentionally flipped: assert `RETRY_RUNTIME_ROOTS_UNRESOLVED`, `blocked_reads >= 1`, zero gateway requests.

## #2600 manual-retry restart stage

The criterion also goes in the PR description.

- **Failed stage.** Use `failed = _canonical_downstream_stage(_failed_stage(state))`, the scope-blind axis the restart router uses (`scheduler_state_failure.py` ~91-98). Do NOT use `_candidate_failed_stage`: it skips model-less cohort rows (~139-141), which is exactly the incident shape (marker on `cycle_ifs_..._convert_dg_...`, failed cycle-scope `state_save_qc` row).
- **Own durable output**, written `own_output`. It is `True` iff either:
  - the candidate's own hydro run status is in `DURABLE_HYDRO_SUCCESS_STATUSES`; or
  - a native-SHUD (`forecast` alias) terminal-success row exists in the state that names the candidate's `model_id` or `run_id`. (Review round 1: `cohort_member_row_is_attributed` never holds for forecast rows, because forecast is excluded from `COHORT_MEMBER_ATTRIBUTED_STAGES`, so that clause is removed. The per-model task projection rows name the model.)

  Details:
  - A row with `cohort_membership == "incomplete"` never counts.
  - The generic state override `durable_shud_output_exists` is **not** trusted by this predicate.
  - It reads the same state `_manual_retry_state_evidence` receives. Report which view that is (`manual_retry_state` vs `decision_state`).
- **Predicate.** Only these parts of `_downstream_retry_evidence` apply: `own_output`, the `_failed_stage` axis, not `_force_native_shud_rerun`, not cold-start-quarantined. Permanence and restartability are ignored, because the marker is the operator's authority. The typical input is `permanently_failed`.
- **Emission.**
  - `failed ∈ {parse, state_save_qc, publish}` and predicate true: `restart_stage = restart_from_stage = failed`, `native_shud_resubmitted=False`, `durable_shud_output_reused=True`.
  - `failed == "forecast"` and not cold-start and the recorded forecast error code is not in the `FORCING_*` family (review round 1: the runtime raises `FORCING_PACKAGE_CHECKSUM_MISMATCH`, `FORCING_FILE_CHECKSUM_MISMATCH`, `FORCING_FILE_NOT_STAGED`, `FORCING_EMPTY`, … for a bad package that a witness check does not detect, and a full chain regenerates it atomically): `restart_stage = "forecast"`, only when the per-model forcing witness is found (reuse `_strict_warm_start_forcing_witness_decision` / the planned-retry guard ~595). If no witness is found, set no restart stage: full chain, **not blocked**.
  - Cold-start quarantined: the existing forced `forecast` branch (~2268-2272), unchanged, with no new guard.
  - Otherwise, no restart stage: full chain.
- **Guards.**
  - The manual branch (`scheduler_state_decision.py` ~309-314) returns today before `_missing_upstream_forecast_artifact_evidence` (~319) and every restart-stage guard.
  - New wiring: the emitted restart stage is checked with the existing upstream-artifact guards for that stage.
  - On any guard failure, drop `restart_stage`/`restart_from_stage` and the reuse flags, and keep the manual retry as a full chain. This preserves the contract at ~826-838 that a manual retry is never stuck behind a blocker.
- **Strict warm-start lane (fixture review round 2).** After the decision, `scheduler_candidates.py` ~691-715 (only when `strict_warm_start is not None`) runs `_upgrade_retry_for_strict_warm_start_manifest`, which may rewrite `restart_stage`, and then `_strict_warm_start_forcing_witness_decision`, which blocks a `forecast` restart without a witness.
  - For a manual-retry decision whose restart stage was added by this change, mark the evidence (e.g. `manual_retry_restart_stage_added: True`).
  - If the post-upgrade witness consultation returns `blocked`, replace it with the pre-upgrade manual-retry decision with the added restart stage dropped (full chain).
  - Cold-start quarantined manual retries (forced `forecast`, pre-existing) keep today's strict-lane behavior.
  - Report what the upgrade does to a `state_save_qc` manual restart.
- **Operator escape (review round 1).** When the own output is provable but the forecast artifacts are actually lost or corrupt, the marker keeps restarting at the failed stage. The runbook documents the escape: restore the artifacts, or first mark the forecast row itself as failed, so that the failed stage is `forecast`, or clear the durable output evidence, so that the full chain runs. The implementer verifies which escape the tooling actually supports, and documents only that one.
- **Known limitation, stated in the PR.** No existence probe checks the forecast artifacts before a `state_save_qc` restart; the copyback guard covers only `copyback`. If the output vanished, the restarted `state_save_qc` fails in its own governed way.
- **End to end.**
  - Decision evidence can be overwritten later by `raw_manifest_restart` (`scheduler_candidates.py` ~965-975; `restart_stage=convert`). The operator-reentry rewrite (~1621) may also overwrite it; verify that path.
  - Tests therefore assert the basin manifest's top-level `restart_stage` (`scheduler_candidate_manifest.py` ~241, the only source of `_restart_stage_from_basins`) and the stage sequence the fake gateway actually submitted, not only the decision evidence.
  - Use the `tests/test_cohort_membership_attribution.py` `_run_pass`/`_Runtime` harness, which already runs a file-journal `state_save_qc` resume end to end.
  - Put the marker on the model-less cohort master (the incident shape).
- **Attempt accounting.** `previous_attempt`/`new_attempt` from the marker is unchanged. Report how the restarted stage's retry id is minted, and how it interacts with #2542 per-member floors (forecast restart only).

## Must-preserve

- Runtime-root evidence without a blocked read is byte-identical: the key is emitted only when > 0. That covers the DB lane and the file-lane golden.
- No-blocked-read resolution is byte-identical, including the environment fallback.
- The four existing blocked consumers keep their semantics, except the intentionally flipped runtime-root degrade outcome: a blocked provenance read now refuses instead of submitting with env roots (see the flipped test above).
- A cold-start quarantined manual retry still restarts at `forecast`, forced native.
- Convert/forcing/unknown-stage manual retries are identical to origin.
- Automatic retries are unchanged.
- The manual-retry marker is unchanged. So is the membership bypass at `scheduler_state_decision` ~283.
- A manual retry is never turned into a blocker by this change.
