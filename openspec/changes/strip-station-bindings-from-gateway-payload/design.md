## Design

**Change surface:** `gateway_wire_entries` in `services/orchestrator/chain_manifests.py:179`. Its three call sites (`chain_manifests.py:243`, `:297`, `chain_stage_execution.py:412`) stay as they are, so every wire copy #3095 covers gets the new projection.

**Must preserve:**
- **#3095 behaviour.** `state_evidence` is still removed from each entry.
- **Sibling keys.** Every `resource_profile` key other than `direct_grid_forcing` is the same object as before. Every `direct_grid_forcing` key other than `station_bindings` and `stations` is kept, including `binding_uri`, `binding_checksum`, `grid_id`, `grid_signature`, `sp_att_path`, `sp_att_checksum`, `model_input_package_id`, `z_policy`, `applicable_source_ids`, `coordinate_reference_system` and `forcing_mapping_mode`. `resource_profile.package_checksum` is kept (state_cli fallback, `packages/common/state_cli.py:531-556`).
- **No mutation.** The input entries, their `resource_profile` and `direct_grid_forcing` mappings, and the station list are unchanged after the call. `context.active_basins` still carries the full table, so in-process readers (the per-run forecast runtime manifest built at `chain_manifests.py:359-402`, candidate evidence, `forcing_member_tasks`) see no change.
- **Pass-through.** If `resource_profile` is missing or not a mapping, or `direct_grid_forcing` is missing or not a mapping, the entry is copied without `state_evidence` and otherwise untouched. No error is raised; validation of these blocks belongs to the scheduler.
- **Other entry keys** are byte-identical.

**Must add / change:**
- `gateway_wire_entries` builds a shallow copy of each entry without `state_evidence`. When `entry["resource_profile"]` is a mapping whose `direct_grid_forcing` is a mapping that contains `station_bindings` or `stations`, it replaces `resource_profile` with a shallow copy whose `direct_grid_forcing` is a shallow copy without those two keys. Otherwise `resource_profile` stays the same object.
- Update the docstring to name both removed fields and the reason (#3099).

**Governing invariant:** no payload sent to the Slurm gateway, and no manifest index file the orchestrator writes, contains `state_evidence` or a direct-grid station binding table; the orchestrator's in-memory basins still contain both.

**Sibling surfaces (no code change):**
- The gateway's `submit_job_array`, `write_manifest_index` and template render (`real_backend.py:353-390,643-690,770-781,864`).
- `infra/sbatch/*_array.sbatch`, `workers/forcing_producer/cli.py`, `workers/output_parser/cli.py`, `workers/shud_runtime/cli.py`, `packages/common/state_cli.py`.
- File-journal manual retry readback (`file_orchestration_journal.py:896-927`): re-submits the smaller tasks.
- Indexes written before deployment still contain the table and are read as before.

**Seams under test:**
- **Projection unit** (`gateway_wire_entries` alone):
  - removes `station_bindings` and `stations` and `state_evidence`;
  - keeps every other `direct_grid_forcing` key and every other `resource_profile` key, including `package_checksum`;
  - does not mutate the input entry, `resource_profile`, `direct_grid_forcing` or the list;
  - passes through entries with no `resource_profile`, a non-mapping `resource_profile`, no `direct_grid_forcing`, a non-mapping `direct_grid_forcing`, or a `direct_grid_forcing` with neither key.
- **Wire capture**: extend the #3095 tests in `tests/test_state_save_submit_ambiguity.py` (`_hlj_cohort_context`, `_gateway_records`, `_submit_with_evidence`) so the basins carry a `direct_grid_forcing` with `station_bindings`. Assert that the gateway `tasks[*]`, `manifest["manifest_index"][*]` and the written `<stage>_manifest_index.json` carry no `station_bindings`, keep `binding_uri` / `binding_checksum`, and that `context.active_basins` still has the table.
- **Publish**: the publish stage manifest `basins` carry no `station_bindings`.

**Required evidence:**
- The tests above.
- The existing array submission, forced-resubmit, manual-retry and restart-stage suites stay green.
- **node-22 receipt:** after deployment, the next 65-task forecast index is well below 5.95 MB, and index-write to gateway `201` is no slower than 7–8 s.

**Non-goals:**
- Registry storage of `station_bindings`.
- `canonical_grid_snapshot` and other small `resource_profile` keys.
- The `stations`-only alias read at `scheduler_candidate_execution_evidence.py:539` (`station_count` missing; separate pre-existing defect).
- Gateway validation de-duplication.

**Review focus:**
- No aliasing: the projection never writes into a mapping it did not create.
- No wire consumer reads `station_bindings` / `stations` from an entry.
- Sibling keys survive byte-identically.
