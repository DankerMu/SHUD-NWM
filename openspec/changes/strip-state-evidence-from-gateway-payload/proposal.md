## Why

Every array task the orchestrator sends to the Slurm gateway carries the candidate's full `state_evidence`. That includes `pipeline_events`, `pipeline_jobs`, `canonical_readiness` and `production_identity_validation`, and the event list has no bound (#3095).

On node-22 on 2026-11-07:
- the 65-task `state_save_qc` index grew from 9.46 MB to 17.75 MB, and the orchestrator's own copy reached 18.67 MB;
- the gateway spent about 55 s of CPU per submit re-scanning it;
- each timeout appends events, so the next request is larger.

The same entries also go out a second time, inside the stage manifest's `manifest_index` field (and `basins` for publish).

Nothing outside the orchestrator process reads `state_evidence`: packages, workers, infra/sbatch and services/slurm_gateway contain no reader. File-journal manual retry reads back only the forcing, forecast, parse and hindcast indexes (`file_orchestration_journal.py:577-580`, 16 MiB cap) and re-submits the same tasks. The projection only makes those files smaller.

## What Changes

- **New projection.** `gateway_wire_entries(entries)` in `services/orchestrator/chain_manifests.py` returns shallow copies of the entries without the `state_evidence` key.
- **Where it applies.** Every payload that crosses the gateway boundary or is written as a manifest index goes through it:
  - the array `tasks` built in `chain_stage_execution.py` (sent to the gateway, and written to `runs/<run_id>/input/<stage>_manifest_index.json`);
  - the stage manifest's `manifest_index` list;
  - the stage manifest's publish `basins` list.
- **Unchanged.** `context.active_basins` and every in-process reader of `basin["state_evidence"]` keep the full evidence. Quality states are still computed from the unprojected entries.

## Triage

```text
Issue type: bugfix (perf)
Fixture level: expanded
Upstream suggested level: absent
Blast radius: a wire consumer that needed state_evidence breaks (none found); projecting in-process data instead breaks restart/manual-retry decisions; missing one copy leaves the payload unbounded
Selected risk packs: Schema / field names (wire payload); Legacy compatibility; Resource limits; Error handling (manual-retry readback)
Evidence floor: uv run ruff check . ; targeted pytest ; node-27 targeted pytest ; node-22 receipt (index size + gateway submit latency)
```

## Impact

- **Code:** `services/orchestrator/chain_manifests.py` and `services/orchestrator/chain_stage_execution.py`.
- **Tests:** new projection and bounded-size tests, plus the existing array submission and manual-retry tests.
- **Spec:** `slurm-job-chain`.
