# Tasks

`deletion-map.md` in this change has file:line evidence (computed on master 8a933c9ec for a wider variant in which the docker validator was also deleted). Where the map and this file disagree, this file wins: the docker lane is kept, and the test lists in section 2 are authoritative.

## 1. Code

- [x] 1.1 Delete the 35 lane modules (map B.2, B.3), `scripts/validate_two_node_e2e_evidence.py`, the m24 scripts and `services/m24_live/`; remove the `nhms-production` console script and the per-file ruff ignores for deleted files from `pyproject.toml`.
- [x] 1.2 Import closure: after 1.1 nothing kept may import a deleted module (static, relative, `importlib.import_module` string, or a `monkeypatch.setattr("services.production_closure.<deleted>...")` string). Known edges: `tests/test_two_node_docker_runtime.py:20-25` and `tests/test_identity_matching.py:7` (handled in 2.2). `scripts/validate_two_node_docker_runtime.py`, `scripts/validate_two_node_docker_source_trust.py`, `scripts/validate_readonly_db_boundary.py`, `scripts/node27_c4_production_acceptance.py`, `services/production_closure/readonly_db_*.py`, `apps/api/**`, `scripts/governance/**` and `tests/conftest.py` have none today.
- [x] 1.3 Reword or drop comments/docstrings in live code that name deleted modules (map B.5; includes `services/orchestrator/scheduler_lease.py:122`, `services/orchestrator/scheduler_evidence.py:33-35`, `scripts/node22_scheduler_evidence_retention.py:219-220`, `tests/test_gateway_reconcile_file_cohort_projection.py:309,371`). Shorten `services/production_closure/AGENTS.md` to the kept C2/C4 modules while keeping the references, commands and glossary link that `scripts/governance/entropy_audit/schema.py:241-257` requires (today at `AGENTS.md:4-16,76-77`); drop the lane entries (`:84,91`). `schema.py` itself does not change.

## 2. Tests

- [x] 2.1 Delete these 16 files whole:
  - `tests/test_m24_emit_baseline.py`, `tests/test_m24_gateway_proof.py`, `tests/test_m24_receipt.py`
  - `tests/test_production_e2e_validation.py`, `tests/test_production_met_validation.py`, `tests/test_production_ops_validation.py`, `tests/test_production_readiness_validation.py`, `tests/test_production_scale_validation.py`, `tests/test_production_slurm_validation.py`
  - `tests/test_slurm_gateway_ops_auth_evidence.py`, `tests/test_slurm_gateway_ops_dependency_closure.py`, `tests/test_slurm_gateway_ops_dependency_hardening.py`
  - `tests/test_two_node_e2e_evidence.py`, `tests/test_readonly_db_two_node_consumer.py`, `tests/test_object_store_validation_facade_contract.py`
  - `tests/test_production_object_store_validation.py`, after its 8 live tests are re-homed per D3 (the eighth, `test_local_object_store_read_bytes_limited_uses_bounded_read`, named `ObjectStoreError` through the lane module's re-export and was caught at orchestrator verification) and the schema-generation shape test is re-homed per D9.
- [x] 2.2 Edit these kept files; remove only what is named, as narrowly as D10 says:
  - `tests/test_two_node_docker_runtime.py`: the two imports at `:20-25` and `test_two_node_e2e_evidence_rejects_display_scheduler_root_env`.
  - `tests/test_identity_matching.py`: the import at `:7` and `test_the_two_node_lane_keeps_its_private_names_bound_to_the_shared_helper`.
  - `tests/test_role_boundary_static.py`: only the assertions/tests that name deleted `services/production_closure` paths; the `docker_runtime` import at `:12` stays.
  - `tests/test_river_ts_read_path_surrogate_keys.py` per D5.
  - `tests/test_status_vocab_parity.py`: the import at `:49`, the two `_CANONICAL_SITES` rows at `:368,371`, and in `test_non_orchestrator_follow_up_sites_rebind_the_vocabulary` the `slurm_validation` tuple at `:566` and the two readiness assertions at `:572-573` (docstring adjusted); the `run_qhh_continuous` rows and assertions stay.
  - `tests/test_operator_action_reservation_lease.py`: the `scripts/m24_lease_nfs_proof.py` list item at `:567`; the `file_orchestration_migration.py` assertion stays.
  - Function-level imports of deleted modules (found by the E1 dry run):
    - `tests/test_orchestration_chain.py:2729-2738` — the `slurm_validation` assertion block at the end of `test_sibling_poll_interval_configs_are_untouched_by_the_orchestrator_floor`; the test stays.
    - `tests/test_safe_fs.py` `test_met_evidence_lane_reports_a_structured_evidence_code` — whole test.
    - `tests/test_scheduler_evidence_summary_lanes.py` `test_readiness_reads_the_compact_summary_pass_exactly_like_the_full_pass` — whole test.
    - `tests/test_scheduler_evidence_decidability.py` `test_a_summary_tier_submitted_pass_still_passes_the_readiness_reader` — its readiness-reader assertions go. Its live assertions (compaction mode `non_blocking_summary`, `model_run_evidence` persisted verbatim) stay unless another surviving test already makes them, in which case the whole test goes and the ledger names that test; if it stays, `_live_scheduler_payload` is re-homed per D3.
  - Any further kept test that the E1 scan shows referencing deleted code: same rule (D10).
  - Every removal in a kept file is recorded in `openspec/changes/delete-production-closure-lanes/deleted-tests-in-kept-files.txt` (see E3).
- [x] 2.3 Must stay whole: every `tests/test_readonly_db_*.py` not listed in 2.1, `tests/test_real_readonly_*.py`, `tests/test_pipeline_ops_identity_envelope.py`, `tests/test_two_node_docker_source_trust.py`, `tests/test_two_node_docker_runbook_environment_invariant.py`, `tests/test_two_node_22_e2e.py`, `tests/test_production_scheduler.py`.
- [x] 2.4 Update pinned data: `tests/status_literal_allowlist.json` (stale entries out), `tests/any_ratchet_ceiling.json` (D4: the exact `count_any` of the final tree), forcing-template census/registry (`tests/forcing_ts_template_registry.py`, `tests/test_forcing_ts_template_census.py`), `scripts/governance/write_entropy_baseline.py` static table and a regenerated `.entropy-baseline/latest.json`, `scripts/ci/full_test_durations.json` (entries of deleted and renamed test files out).

## 3. Docs

- [x] 3.1 Delete `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md`, `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`, `docs/validation/production-closure.md`. Edit every live doc that cites a deleted file, test or command (map C.3, H.5): `README.md`, `docs/VALIDATION.md`, governance inventories, `infra/env/README.md`, `workers/forcing_producer/AGENTS.md`, `docs/runbooks/node-27-bringup-checklist.md:295` (the "twelve-lane aggregator" mention), and the runbooks that tell the operator to run `nhms-production validate-*` — `api-latency.md`, `failed-basin-retry.md`, `object-store-failure.md`, `slurm-backlog.md`, `source-latency.md`, `stale-analysis.md`, `tile-publish-error.md`, `production-service-config.md`, plus `qhh-mvp-smoke-evidence.md`. In an alert runbook the deleted command is removed, not replaced by another validator; the alert's own diagnosis steps stay.

## 4. Specs and archive (orchestrator)

- [x] 4.1 Spec deltas; `openspec validate delete-production-closure-lanes --strict --no-interactive`.
- [x] 4.2 Archive in the final push; hand-delete the seven retired spec directories `openspec archive` cannot retire; Purposes of touched specs; comment on #3231 with the residual references it inherits.

## Evidence Floor

- [x] E1 Import closure: an AST scan over all tracked `*.py` finds no import (static, relative, `importlib.import_module` string) of a deleted module, and no string literal `services.production_closure.<deleted stem>` or `services.m24_live`; `uv run pytest tests/ -q --collect-only` collects with no error.
- [x] E2 Zero references, checked after archive over `git ls-files`, excluding D7's frozen paths and `openspec/changes/archive/**`:
  - (a) for every path in `git diff --no-renames --name-only --diff-filter=D master`: the full path has no hit; the basename has no hit as a whole token (not preceded by `[A-Za-z0-9_]`) when that basename is unique among master's tracked files;
  - (b) for every deleted `services/production_closure/<stem>.py`: `production_closure[./]<stem>\b` has no hit;
  - (c) these have no hit: `nhms-production(\s+validate-|\s*=|`)`, the bare sub-command `validate-(ops|met|e2e|scale|slurm|object-store|readiness)` as a whole word (added at PR review), `services[./]m24_live`, `scripts/m24_`, `m24_(emit_baseline|gateway_proof|lease_nfs_proof)`, `READINESS_VALIDATION_LANE_INVENTORY`, `TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY`, `docs/validation/production-closure`;
  - (d) the five residual files the proposal hands to #3231 are exempt from (a)-(c); nothing else is.
  - The check is `artifacts/issue-3221/check_3221.py` (kept outside git; its rules are exactly (a)-(d) and E1/E3).
- [x] E3 Live tests preserved. `before` = ids collected at the fixture commit (23,245 ids; sha256 `0e465c0310cc6c9de0f4eb1d9cead41688d9ff254f6dbc40bc6c02e1f993c58f`), `after` = ids collected at the final commit (`uv run pytest tests -q --collect-only`). Then: every id in `before - after` belongs to a file in 2.1 or is listed in `openspec/changes/delete-production-closure-lanes/deleted-tests-in-kept-files.txt` (committed; section 1: removed test ids, one per line, each naming the deleted module in a trailing comment; section 2: tests edited in place whose id is unchanged, with the removed rows/assertions); `after - before` is exactly the re-homed object-store tests (the 8 live tests and the D9 shape test), each matched one-to-one to an old id by function name and parametrisation.
- [x] E4 C2/C4 sources untouched: `git diff master -- services/production_closure/c4_production_acceptance*.py services/production_closure/readonly_db_*.py services/production_closure/identity_matching.py scripts/validate_readonly_db_boundary.py scripts/node27_c4_production_acceptance.py apps/api/readonly_validation_probe.py` is empty; `git diff --stat master -- tests/test_readonly_db_*.py tests/test_real_readonly_*.py tests/test_pipeline_ops_identity_envelope.py` shows only the deletion of `tests/test_readonly_db_two_node_consumer.py`; the C2/C4 test files pass.
- [x] E5 `uv run ruff check . --no-cache`; ratchet and scan suites pass (`tests/test_any_ratchet.py`, `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_time_helper_consolidation.py`, `tests/test_import_layering_guard.py`, all `tests/test_entropy_audit_*.py`, `tests/test_role_boundary_static.py`, `tests/test_forcing_ts_template_census.py`); `tests/any_ratchet_ceiling.json` equals the `count_any` of the final tree; re-running `scripts/governance/write_entropy_baseline.py` leaves `.entropy-baseline/latest.json` unchanged apart from its timestamp/commit fields; markdownlint on changed docs.
- [x] E6 Full sharded pytest green on the PR; Production Topology Hard Gate green.
- [x] E7 `uv.lock` unchanged.
- [ ] E8 (post-merge, both nodes, recorded on #3221; pending when this change was archived):
  - node-27 after `git pull --ff-only`: `nhms-display-api.service` restarted and `/health` is 200; a C4 acceptance receipt produced on the merged head with `scripts/node27_c4_production_acceptance.py`; `scripts/validate_readonly_db_boundary.py` run far enough to load `apps/api/readonly_validation_probe.py` (a real read-only run, not `--help`); `systemctl --user cat` of every installed `nhms-*` unit names no deleted file.
  - node-22 after `git pull --ff-only` at a scheduler pass boundary (no `uv sync`, no bare `uv run`): compute API and Slurm gateway restarted and healthy; `systemctl --user cat` of every installed `nhms-*` unit names no deleted file; `.venv/bin/python -c "import apps.api.main, services.orchestrator.cli, services.slurm_gateway"` succeeds; one scheduler pass ends `planned` with `root_preflight ready`.
