## Design

**Change surface:**
- **`chain_manifests.py`:**
  - new `gateway_wire_entries`;
  - in `build_cycle_stage_manifest`, `manifest["manifest_index"]` (~:212) and the publish `manifest["basins"]` (:286).
- **`chain_stage_execution.py`:** the `tasks` passed to `_write_cycle_manifest_index` and `_submit_array_stage` (~:411).

**Must preserve:**
- **In-process data.** `context.active_basins` keeps `state_evidence` intact. It is not mutated, because the projection copies.
- **In-process readers.** Every `basin["state_evidence"]` read stays as it is:
  - `chain_runtime_utils.py` (decision, restart_stage, manual_retry, retry_policy, completed_stage_evidence);
  - `chain_forced_resubmit.py`;
  - `chain_forecast_cycle.py` (journal_predecessor_identity);
  - `chain_forecast_execution.py:650` and `:906`, plus `retry_identity.cohort_retry_attempt_floors` at `chain_forecast_orchestrator_cycle.py:741` (retry_attempt_floor);
  - `accepted_submit_identity.py:1235,1280` (quarantine and budget provenance, called from `chain_forecast_orchestrator_cycle.py:705-713`);
  - `chain_forecast_cycle._state_evidence_layers` (`:341`).
- **Quality states.** `build_cycle_stage_manifest` still computes quality_states, residual_blockers, run_ids and model_ids from the unprojected entries.
- **Other entry keys** are byte-identical, including task_id, original_task_id, model_run_assembly and the URIs.
- **Reservation.** The forcing reservation `forcing_member_tasks` (chain_stage_execution.py:359) is not a wire payload and stays unprojected.
- **Manual retry.** `_file_manual_retry_array_tasks` still returns the index tasks. They no longer carry state_evidence, which no gateway or compute reader needs.

**Must add / change:**
- `gateway_wire_entries(entries: Sequence[Mapping]) -> list[dict]` returns `[{k: v for k, v in e.items() if k != "state_evidence"} for e in entries]`. It removes only `state_evidence`, with no other filtering.
- Apply it to the three wire copies above, and nowhere else.

**Governing invariant:** no payload sent to the Slurm gateway, and no manifest index file the orchestrator writes, contains `state_evidence`; the orchestrator's in-memory basins still do.

**Sibling surfaces:**
- **Gateway `real_backend.submit_job_array`** and its own index write receive the projected tasks. No code change.
- **The non-array `submit_job` path** goes through `_slurm_submission_manifest(stage_manifest)`. It is covered, because stage_manifest's `manifest_index` and `basins` are projected.
- **The file-journal manual retry** reads the projected orchestrator index.
- **The mock backend** reads `manifest.tasks` or `basins` for task identity only.
- **sbatch templates and `packages/common/state_cli.py`** read identity fields only.

**Seams under test:**
- **`gateway_wire_entries`** on its own: the key is removed, other keys are kept, and the input is not mutated.
- **A real `ForecastOrchestrator` array stage**, using `tests/test_state_save_submit_ambiguity.py` `_hlj_orchestrator`, `_hlj_cohort_context` and `_submit_stage` (:82-136):
  - give the basins a `state_evidence` with `pipeline_events`, because `_hlj_cohort_context` has none by default;
  - capture the real HTTP JSON by making `_gateway_answers` (:62) or `_scripted_gateway` (:313) record it. `FakeCycleSlurmClient.submissions` in `tests/test_orchestration_chain.py:63` is an alternative.

  Assert that:
  - no `tasks[*]` and no `manifest["manifest_index"][*]` contains state_evidence;
  - the written `runs/<run_id>/input/<stage>_manifest_index.json` has none;
  - `context.active_basins` still has it.
- **Bounded size:** a candidate whose `state_evidence.pipeline_events` has N=1 versus N=500 entries produces byte-identical serialized wire tasks.

**Required evidence:**
- The tests above.
- The existing array submission, forced resubmit, manual retry and restart-stage suites stay green.
- **node-22 receipt:** after deployment, the next 65-task array index is far below 17.75 MB, and the gateway submit takes far less than 55 s from POST to 201.

**Non-goals:**
- Gateway validation de-duplication (issue alternative B).
- `resource_profile` size.
- Journal or evidence storage growth.
- The client timeout (#3094).

**Review focus:**
- No wire copy is missed. Check every `_reindexed_manifest_entries` / `active_basins` value that reaches `slurm_client` or a file.
- Nothing downstream of the wire needs state_evidence.
- In-process basins are not mutated.
