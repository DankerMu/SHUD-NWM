## Risk packs

- **Schema / field names (wire payload) — selected.** `station_bindings` / `stations` are removed from `resource_profile.direct_grid_forcing` in gateway tasks and index files. Covered by the projection and wire-capture tests.
- **Legacy compatibility — selected.** Other keys and in-process basins are unchanged; old indexes still read. Covered by sibling-key assertions, the active_basins assertion and the existing manual-retry and forced-resubmit suites.
- **Resource limits — selected.** Wire size no longer scales with the station count. Covered by the wire-capture test and the node-22 receipt.
- **Concurrency / shared state — selected.** Copy-on-write; shared candidate and registry dicts must not be mutated. Covered by the no-mutation assertions.
- **Not selected:**
  - Public API / CLI: none.
  - Config: none.
  - File IO / path safety: same paths, smaller content.
  - Auth: none.
  - Error handling: malformed blocks pass through; no new error path.
  - Release: no dependency change.
  - Documentation: no documented contract lists station_bindings on the wire.

## Evidence Floor

- `uv run ruff check .`
- `uv run pytest -q tests/test_state_save_submit_ambiguity.py tests/test_orchestration_chain.py tests/test_forcing_submit_ambiguity.py tests/test_manual_retry_failed_stage_restart.py`
- node-27 targeted pytest on the PR head
- node-22 receipt: the next 65-task forecast index file size, and index-write to gateway `201` latency, compared with 5.95 MB and 7–8 s

## Tasks

- [x] 1.1 Extend `gateway_wire_entries` in `chain_manifests.py` to drop `resource_profile.direct_grid_forcing.station_bindings` and `stations`, copy-on-write, with pass-through for absent or non-mapping blocks; update its docstring.
- [x] 1.2 Tests:
  - projection unit: removal, sibling keys, `package_checksum`, no mutation, pass-through cases (including a `direct_grid_forcing` with neither key, where `resource_profile` stays the same object);
  - add a `resource_profile=` kwarg to `_hlj_cohort_context`; `_submit_with_evidence` asserts on `state_evidence`, so new wire tests pass both;
  - wire capture: gateway tasks, `manifest_index` and the written index file carry no station table; `binding_uri` / `binding_checksum` kept; in-process basins intact;
  - publish stage manifest `basins` carry no station table.
- [x] 2.1 Ruff, targeted pytest locally, and node-27 targeted pytest.
  - Local: ruff clean; the four Evidence Floor suites 622 passed, 1 skipped. node-27, PR head `a4317d0e0`, detached worktree: `3422 passed, 1 skipped in 674.97s` (PR #3100 comment).
- [x] 3.1 node-22 receipt (post-merge).
  - Deployed `69dc8d34c` on 2026-11-08T02:07Z. First post-deploy cycle `2026-10-17T12Z`: 65-task forecast index 5.95 MB -> 0.78 MB, forcing 5.93 MB -> 0.76 MB; index-write to gateway `201` 7–8 s -> 2 s; 0 entries carry `station_bindings`/`stations`, `binding_uri` and `package_checksum` kept 65/65. Receipt in the PR #3100 comment.
