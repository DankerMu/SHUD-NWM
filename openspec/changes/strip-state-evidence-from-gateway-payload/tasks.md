## Risk packs

- **Schema / field names (wire payload) — selected.** `state_evidence` is removed from the gateway tasks and the index files. Covered by the projection and wire-capture tests.
- **Legacy compatibility — selected.** In-process readers are unchanged. Covered by the active_basins assertion and the existing restart, manual-retry and forced-resubmit suites.
- **Resource limits — selected.** The payload is now bounded and independent of the event count. Covered by the N=1 vs N=500 byte-identity test.
- **Error handling — selected.** The manual-retry readback of the forcing, forecast, parse and hindcast indexes returns the same tasks, minus state_evidence. Covered by the existing manual-retry tests. The node-22 receipt measures state_save_qc, which manual retry never reads.
- **Not selected:**
  - Public API / CLI: no new entry.
  - Config: none.
  - File IO / path safety: same paths, smaller content.
  - Auth: none.
  - Concurrency: no shared state change.
  - Release: no dependency change.
  - Documentation: no documented contract lists state_evidence on the wire.

## Evidence Floor

- `uv run ruff check .`
- `uv run pytest -q` on the projection and wire tests, plus `tests/test_state_save_submit_ambiguity.py`, `tests/test_orchestration_chain.py` and the manual-retry and forced-resubmit suites
- node-27 targeted pytest on the PR head
- node-22 receipt: the next 65-task index file size, and gateway POST-to-201 latency, compared with 17.75 MB and ~55 s

## Tasks

- [x] 1.1 Add `gateway_wire_entries` to `chain_manifests.py`.
- [x] 1.2 Apply it to the array `tasks` in `chain_stage_execution.py` (index write and submit).
- [x] 1.3 Apply it to `manifest["manifest_index"]` and the publish `manifest["basins"]` in `build_cycle_stage_manifest`. Computations stay on the unprojected entries.
- [x] 1.4 Tests:
  - the projection unit;
  - the wire capture of tasks, manifest_index and the index file, with in-process basins intact;
  - N=1 vs N=500 byte identity.
- [ ] 2.1 Ruff, targeted pytest locally, and node-27 targeted pytest.
- [ ] 3.1 node-22 receipt (post-merge).
