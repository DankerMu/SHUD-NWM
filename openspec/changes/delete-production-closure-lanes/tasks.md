# Tasks

`deletion-map.md` in this change has file:line for every item (computed on master 8a933c9ec; "variant B" is the one implemented, with the docker lane kept per the proposal). Section letters below refer to it.

## 1. Code

- [ ] 1.1 Delete the 35 lane modules (map B.2, B.3), `scripts/validate_two_node_e2e_evidence.py`, the m24 scripts and `services/m24_live/`; remove the `nhms-production` console script and the per-file ruff ignores for deleted files from `pyproject.toml`.
- [ ] 1.2 Resolve D2: recompute the import closure after 1.1 for `scripts/validate_two_node_docker_runtime.py`, `scripts/validate_two_node_docker_source_trust.py`, `scripts/validate_readonly_db_boundary.py`, `scripts/node27_c4_production_acceptance.py`, `apps/api/**` and their tests; nothing may import a deleted module.
- [ ] 1.3 Reword or drop comments/docstrings in live code that name deleted modules (map B.5); shorten `services/production_closure/AGENTS.md` to the kept C2/C4 modules.

## 2. Tests

- [ ] 2.1 Delete the test files that exist only for deleted code (map C.4 "variant B" list and F; the docker-lane tests stay).
- [ ] 2.2 Split the mixed files (map C.4 table, H.2, H.3): remove only the tests of deleted code; re-home helpers per D3; handle D5.
- [ ] 2.3 Update pinned data: `tests/status_literal_allowlist.json`, `tests/any_ratchet_ceiling.json` (D4), forcing-template census/registry, entropy baseline + writer table + scoped-AGENTS config and their tests, `scripts/ci/full_test_durations.json`.

## 3. Docs

- [ ] 3.1 Delete the two lane inventories and `docs/validation/production-closure.md`; edit every live doc that cites a deleted file or test (map C.3, H.5), including `README.md`, `docs/VALIDATION.md`, runbooks, governance inventories, `infra/env/README.md`, `workers/forcing_producer/AGENTS.md`.

## 4. Specs and archive (orchestrator)

- [ ] 4.1 Spec deltas; `openspec validate delete-production-closure-lanes --strict --no-interactive`.
- [ ] 4.2 Archive in the final push; Purposes of touched specs.

## Evidence Floor

- [ ] E1 Import closure: an AST scan over all tracked `*.py` finds no import (static, relative, or `importlib.import_module` string) of a deleted module; `uv run pytest tests/ -q --collect-only` collects with no error.
- [ ] E2 Zero-reference grep for the deleted module and script names (after archive; excluding D7's frozen paths and the docker-lane files named in "Out of scope") returns nothing: `slurm_validation`, `e2e_validation`, `met_validation`, `ops_validation`, `scale_validation`, `object_store_validation`, `readiness_validation`, `readiness_dependency`, `two_node_e2e_evidence`, `two_node_e2e_`, `nhms-production`, `m24_emit_baseline`, `m24_gateway_proof`, `m24_lease_nfs_proof`, `m24_live`, `validate_two_node_e2e_evidence`, `READINESS_VALIDATION_LANE_INVENTORY`, `TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY`, `docs/validation/production-closure`.
- [ ] E3 Live tests preserved: for every split file, the set of collected test ids after the change equals the set before minus the tests listed as deleted-lane tests (script output attached to the PR).
- [ ] E4 C2/C4 untouched: `git diff master -- services/production_closure/c4_production_acceptance*.py services/production_closure/readonly_db_*.py services/production_closure/identity_matching.py scripts/validate_readonly_db_boundary.py scripts/node27_c4_production_acceptance.py apps/api/readonly_validation_probe.py` is empty; their test files pass.
- [ ] E5 `uv run ruff check . --no-cache`; ratchet and scan suites pass (`tests/test_any_ratchet.py`, `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_time_helper_consolidation.py`, `tests/test_import_layering_guard.py`, all `tests/test_entropy_audit_*.py`, `tests/test_role_boundary_static.py`, `tests/test_forcing_ts_template_census.py`); markdownlint on changed docs.
- [ ] E6 Full sharded pytest green on the PR; Production Topology Hard Gate green.
- [ ] E7 `uv.lock` unchanged.
- [ ] E8 (post-merge, both nodes) After `git pull --ff-only`: node-27 display API restarts and `/health` is 200, `scripts/validate_readonly_db_boundary.py --help` and `scripts/node27_c4_production_acceptance.py --help` run; node-22 compute API and gateway restart healthy and one scheduler pass ends `planned` with `root_preflight ready`. Recorded on #3221.
