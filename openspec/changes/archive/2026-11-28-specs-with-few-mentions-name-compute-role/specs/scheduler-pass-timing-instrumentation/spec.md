## MODIFIED Requirements

### Requirement: Candidate-layer timing is emitted only at `NHMS_SCHEDULER_TIMING_LEVEL=candidate`

At level `candidate`, the scheduler SHALL emit one candidate-layer record per (basin_model_id, source_id, stage_name) submitted within the pass. Including `source_id` in the key is deliberate: the same basin_model_id runs against both `gfs` and `IFS` sources in a single pass, and their sub-phase timings must not collapse into one record.

Each candidate record SHALL include the following fields corresponding to concrete code regions:

- Keys: `model_id`, `basin`, `source_id`, `stage_name`
- Sub-phases inside `services/orchestrator/scheduler_execution.py execute_candidate_cohort` (L233-410): `output_uri_lookup_ms` (L248-273), `basin_manifest_build_ms` (L276-283), `slurm_env_check_ms` (L289-303), `secret_manifest_scan_ms` (L305-308), `resource_profile_check_ms` (L309-322), `stage_raw_input_ms` (L329), `orchestrator_dispatch_ms` (L358 `orchestrator.orchestrate_cycle`) — **the first five (`output_uri_lookup_ms`, `basin_manifest_build_ms`, `slurm_env_check_ms`, `secret_manifest_scan_ms`, `resource_profile_check_ms`) are per-basin, opened inside the per-basin inner loop**; **`stage_raw_input_ms` (L329) and `orchestrator_dispatch_ms` (L358) are per-cohort, measured once at cohort scope and attributed per basin as an equal share for accounting simplicity**. This partition — 5 per-basin + 2 per-cohort — is canonical across `spec.md`, `tasks.md` §2.4, and issue #862.
- Sub-phases inside `services/orchestrator/chain_forecast_execution.py _submit_and_wait` (L489-599, invoked by `orchestrate_cycle` per stage per model): `build_stage_manifest_ms` (L498), `submit_sbatch_ms` (L505, direct-measured Slurm wait), `poll_until_terminal_ms` (L574-582 when the initial status is non-terminal; `0` and the `submit_sbatch_ms` span covers the wait when the fast path at L568-572 fires), `post_stage_hook_ms` (L590-594 `_after_stage_success` / `_after_stage_failure`).

Candidate-layer records SHALL NOT be written to stdout under any level to prevent operator log flood.

#### Scenario: Candidate level enables candidate records in evidence only

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=candidate` and a pass runs 13 basin models × 2 sources × 5 stages (the canonical workload on the retired node-22 host, recorded no later than 2026-07-25)
- **THEN** the evidence JSON `timing.candidates` array contains 130 records (13 × 2 × 5), one per `(basin_model_id, source_id, stage_name)` tuple
- **AND** each record has non-negative values for every included sub-phase duration
- **AND** the operator's `journalctl` output has zero lines with `level="candidate"`.

#### Scenario: Stage level omits candidate records

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=stage` (default)
- **THEN** the evidence JSON `timing.candidates` is absent or an empty array.
