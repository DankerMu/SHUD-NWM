# Delete the production_closure validation lanes and the m24 chain (#3221)

## Why

`services/production_closure/` holds 45 modules (37.9k lines) and about 31k lines of tests. The validation lanes in it are no
longer an acceptance mechanism (owner decision 2026-11-19). Nothing in CI or in any installed systemd unit on either node runs
them; their only static root is the `nhms-production` console script, which no production path calls. The m24 chain
(`scripts/m24_*.py`, `services/m24_live/`) has no caller outside its own tests.

## What changes

1. Deleted from `services/production_closure/` (35 modules, about 32.7k lines):
   - the `nhms-production` lanes: `slurm_validation` and its fan-out (`e2e_validation`, `met_validation`, `ops_validation`,
     `scale_validation`, `object_store_validation*`, `readiness_*`);
   - the two-node e2e evidence lanes: `two_node_e2e_evidence` and `two_node_e2e_*`.
2. Deleted with them: `scripts/validate_two_node_e2e_evidence.py`, the `nhms-production` console script entry, the m24 chain
   (`scripts/m24_emit_baseline.py`, `scripts/m24_gateway_proof.py`, `scripts/m24_lease_nfs_proof.py`, `services/m24_live/`),
   the tests that exist only for the deleted code, `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md`,
   `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`, `docs/validation/production-closure.md`.
3. Kept in place, source unchanged (`services/production_closure/` shrinks to 10 modules):
   - C4 acceptance: `c4_production_acceptance`, `c4_production_acceptance_io` (root `scripts/node27_c4_production_acceptance.py`);
   - C2 read-only DB validator: `readonly_db_validation`, `readonly_db_route_smoke`, `readonly_db_types`, `readonly_db_merge`,
     `readonly_db_permission_probes`, `readonly_db_probe_adapter`, `identity_matching` (root
     `scripts/validate_readonly_db_boundary.py`; loads `apps/api/readonly_validation_probe.py` dynamically).
4. Tests that mix deleted-lane tests with tests of live code are split: the live tests stay. `tasks.md` section 2 holds the
   authoritative file lists (the deletion map's C.4 table was computed for a wider variant and is not the list to follow).
5. Data and docs that enumerate the deleted files are updated: `tests/status_literal_allowlist.json`,
   `tests/any_ratchet_ceiling.json`, `.entropy-baseline/latest.json` and the writer's static table, the scoped-AGENTS config,
   `scripts/ci/full_test_durations.json`, `pyproject.toml` per-file ignores, the forcing-template census/registry, runbooks,
   governance inventories, `README.md`, `docs/VALIDATION.md`, scoped `AGENTS.md` files.
6. Capability specs whose subject is a deleted lane are retired (12 capabilities, 27 requirements); lane requirements inside
   18 other specs are removed or have their lane clauses cut. Seven of the retired specs use bullet-less `WHEN/THEN` lines that
   `openspec archive` cannot parse for automatic retirement; their directories are deleted by hand in the archive commit.

## Fixture

- Level: expanded (large deletion across shared test files and governance data).
- Must preserve: the API app and every live worker/service/package (no non-test survivor imports a deleted module — verified by
  an AST import closure, deletion-map C.1); C2 and C4 tool sources, byte-identical, and their tests (one exception:
  `tests/test_identity_matching.py` loses its `two_node_e2e_evidence` import and the one test that binds the deleted lane to
  the shared helper); every live test inside the split files; `config/resource_profiles.yaml` (`m24_smoke` is used by live
  scheduler tests); `uv.lock`.
- Out of scope: the docker/compose lane — `scripts/validate_two_node_docker_runtime.py`,
  `scripts/validate_two_node_docker_source_trust.py`, `infra/README.two-node-docker.md`, compose files,
  `tests/test_two_node_docker_*` (moved to #3231); relocating the kept modules; the `production_closure_*` run ids inside
  `infra/sbatch/run_shud_forecast_array.sbatch`.
- Docker-lane exceptions (the only edits to docker-lane files in this change):
  - `tests/test_two_node_docker_runtime.py` imports `two_node_e2e_docker_security` and `two_node_e2e_evidence` (lines 20-25)
    for one test, `test_two_node_e2e_evidence_rejects_display_scheduler_root_env` (8 parameter cases). The two imports and
    that test are removed; the other tests of the file stay.
  - Left as they are and handed to #3231: `infra/README.two-node-docker.md` (lines 789, 852 tell the operator to run the
    deleted `scripts/validate_two_node_e2e_evidence.py`), `tests/test_two_node_docker_runbook_environment_invariant.py`
    (pins that command text; it compares text only, so it stays green), `infra/docker/entrypoint.sh:43` (deny-list entry),
    `tests/test_two_node_docker_runtime.py:4244`. These four files are the declared residual references of E2.

## Decisions

- D1. The kept modules stay under `services/production_closure/`. No import path, runbook reference or test of C2/C4 changes.
- D2. Because the docker lane stays (#3231), anything it imports from the deleted set must be handled, not broken: if
  `scripts/validate_two_node_docker_runtime.py` or its tests import a deleted module, the import closure decides — a symbol
  needed only by the docker validator moves into that script; nothing else is restored.
- D3. A mixed test file keeps its live tests in place under the same file name; only when every remaining test would be about
  deleted code is the file deleted. Helpers that live tests import from a deleted test module move to a small `tests/*_helpers.py`.
  One exception: `tests/test_production_object_store_validation.py` keeps 7 of 58 tests, none about the deleted lane
  (`LocalObjectStore` / `safe_fs` symlink and FIFO refusals, one packager test). They move, bodies unchanged, to a test file
  named for what they test (an existing object-store / safe_fs test file, or a new one); the old file is deleted.
- D4. `tests/any_ratchet_ceiling.json` is set to the exact count the ratchet's own `count_any` reports on the final tree (the
  ratchet fails more than 5% below the ceiling; the fixture review's recount for this scope is 9372 - 1737 = 7635, the
  deletion map's 7470 assumed the docker validator was deleted). No other ratchet or allowlist is loosened; stale allowlist
  entries are removed.
- D5. Constants that a live test imported from a deleted lane (`scale_validation.HYDRO_MAP_PLAN_LINES_BY_STORE`, `QUERY_TARGETS`
  in `tests/test_river_ts_read_path_surrogate_keys.py`): the two tests go with the lane unless they assert a property of live
  code, in which case the constants move into the test module.
- D6. Comments and docstrings in live code that name a deleted module are reworded or dropped.
- D7. Frozen history is not rewritten: `openspec/changes/archive/**`, `docs/runbooks/receipts/**`, `docs/review-loop-log.jsonl`,
  `docs/adr/**`, dated plans (`docs/plans/**`), dated audit snapshots (`docs/governance/entropy-audit-2026-10-16*`, `.entropy-baseline/2026-*.json`).
- D8. A requirement about live behaviour whose only verifier was a deleted lane is removed with the lane only when a live test
  still pins the behaviour; otherwise the requirement stays and the verifying assertion is re-homed as an ordinary test.
  Settled cases: `fixed-station-forcing-production` "OUTPUT_UNITS" is pinned by `tests/test_forcing_producer.py:5762-5840`;
  non-finite SHUD output rejection (`staging-end-to-end-closure`) is pinned by `tests/test_output_parser.py` (`NON_FINITE_FLOW`,
  `workers/output_parser/parser.py`); the daemon lease heartbeat/compare-and-swap (`continuous-daemon-live-operation`) by
  `tests/test_production_scheduler.py`; the pass-evidence filename predicate (`runtime-evidence-and-operations`) by the
  retention and stall-health tests.

## Reported, not fixed here

- `forcing_checksum_material_for_schema_version` keeps a pre-migration branch whose only caller with an old schema version
  was the deleted object-store validation lane.
- `production_mvt_readiness_claimed` (`apps/api/routes/mvt.py`, OpenAPI) is no longer mentioned by any spec after this change.
