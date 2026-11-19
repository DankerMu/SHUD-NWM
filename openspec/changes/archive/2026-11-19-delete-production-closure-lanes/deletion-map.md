# #3221 deletion map: services/production_closure + dormant validation tooling

Method: AST import graph over all 1283 first-party .py files (import / from-import / relative / function-level / TYPE_CHECKING; string constants naming modules or `scripts/x.py` paths are recorded as "dynamic" and manually verified). Shell/systemd/sbatch/yaml/Makefile scanned for `-m mod`, `scripts/x.py|sh`, and `nhms-*` console names. Helper scripts: scratchpad `graph.py`, `roots.py`, `reach.py`, `q3.py` (tests), `anyc.py` (Any ratchet), `deps.py`.
Repo state: master 8a933c9ec (tree identical for these paths).

## 0. Headline findings (read first)

1. **Live import closure into `services/production_closure` is tiny and has exactly two non-test entry points**:
   - `scripts/node27_c4_production_acceptance.py:14-15` -> `c4_production_acceptance` (`bind, freeze, verify`) + `c4_production_acceptance_io` (`C4AcceptanceError`). Those two modules import NOTHING else from the package (graph: closure = 2 modules, 1361 LOC; plus `packages.common.{evidence_io,safe_fs*,source_identity,contracts.time_normalization,node27_pgdata_command}` which are live anyway).
   - `apps/api/readonly_validation_probe.py:12-24` -> `readonly_db_validation` (10 names, see B.1). That drags in 7 modules (6 `readonly_db_*` + `identity_matching`, 3,847 LOC) **only because the probe imports private helpers from the lane's facade**.
2. **`apps/api/readonly_validation_probe.py` has NO live caller.** Its only loader is a dotted-string constant inside the lane being deleted: `services/production_closure/readonly_db_types.py:13` (`DEFAULT_API_PROBE_ADAPTER_MODULE`), consumed by `readonly_db_validation.py` (via `importlib`). The only other mention is a docs line `docs/governance/ROLE_BOUNDARY.md:327` and a test assert `tests/test_readonly_db_validation_routes.py:300`. Keeping it per the issue is fine, but it becomes an unreferenced module (and the Any ratchet / layering scans still see it). Needs only ~110 LOC of helpers relocated (B.1).
3. **C1/C2 tooling conflict with the issue's delete list (owner decision needed).** `docs/runbooks/node-27-bringup-checklist.md:224-225` names `scripts/validate_readonly_db_boundary.py` -> `services/production_closure/readonly_db_validation` as THE canonical **C2** (readonly DB denied-write receipt) entry; line 126 cites it as the readonly probe framework; `:213` names `scripts/validate_two_node_docker_runtime.py static` for **C1**. `docs/runbooks/tier-node27-timeseries-storage.md:832,888` also cites the C2 validator. CLAUDE.md lists "display 边界 deny-write" as a node-27 oracle. Two variants are costed below: **A** (issue literal: delete everything but the C4 pair) and **B** (A but keep the 6 `readonly_db_*` modules + `identity_matching` (7 modules, 3,847 LOC) + `scripts/validate_readonly_db_boundary.py` = C2 keeps working). The C1 `validate_two_node_docker_runtime.py` is explicitly on the delete list; runbook line 213 then needs rewording.
4. **`nhms-production` is not a production path.** `pyproject.toml:61` -> `slurm_validation:main`. Its only in-repo mention in executable code is `infra/docker/entrypoint.sh:43`, which lists it in `DISPLAY_FORBIDDEN_COMMANDS` (a deny list, not an invocation). No systemd unit, compose file (`infra/compose*.yml`), sbatch, Makefile, or workflow calls it (greps clean). `README.md:252,276` documents it. Deleting the entry only needs the deny-list line to stay (harmless) or be dropped, and `tests/test_two_node_docker_runtime.py` entrypoint tests (which assert the forbidden set) kept consistent.
5. **No third-party dependency becomes unused** (E). **No schema under `schemas/` is owned solely by the lanes** (C). Both reduce the PR: `uv.lock`/`pyproject` deps untouched.
6. **Governance hard-gates that enumerate these paths** (D): `scripts/governance/entropy_audit/schema.py:240-255` (scoped AGENTS.md config for `services/production_closure`), `write_entropy_baseline.py:91-97`, `.entropy-baseline/latest.json:698-720` (file_count 46), `tests/status_literal_allowlist.json` (8 stale entries fail `test_status_literal_scan`), Any ratchet floor (9372 -> 7281 requires ceiling 9500 -> 7281 in the same PR).

## A. Roots (live entry points)

### A.1 pyproject `[project.scripts]` (pyproject.toml:48-61)
| name | target | live? |
|---|---|---|
| nhms-gfs / nhms-era5 / nhms-ifs | workers.data_adapters.cli:main / era5_main / ifs_main | live |
| nhms-canonical | workers.canonical_converter.cli:main | live |
| nhms-forcing | workers.forcing_producer.cli:main | live |
| nhms-model | workers.model_registry.cli:main | live |
| nhms-shud-runtime | workers.shud_runtime.cli:main | live |
| nhms-parse | workers.output_parser.cli:main | live |
| nhms-pipeline | services.orchestrator.cli:main | live |
| nhms-monitor | services.orchestrator.monitoring:main | live |
| **nhms-production** | services.production_closure.slurm_validation:main | **not live** (A.4) |
| nhms-state | packages.common.state_cli:main | live |

### A.2 systemd (infra/systemd/**, non-compose) ExecStart/Pre targets
- python -m: `scripts.node22_state_index_capacity_watch` (nhms-scheduler-state-index-capacity.service:11), `scripts.node22_state_index_auto_prune` (…-prune.service:13), `services.slurm_gateway` (nhms-slurm-gateway.service:80; node22/nhms-slurm-gateway.service:17; node22/…service.d/10-node22-live.conf:9), `services.orchestrator.cli plan-production` (node22/nhms-compute-scheduler.service:13), `uvicorn apps.api.main:app` (nhms-display-api.service:9; node22/nhms-compute-api.service:13).
- python script paths: node22_scheduler_journal_retention.py, node22_scheduler_stall_health.py, node22_refresh_timer_health.py, node22_scheduler_evidence_retention.py, node27_parse_failure_residency_alert.py, node27_coverage_freshness_alert.py, node27_timeseries_compression_supervisor.py (ExecStart + ExecStopPost, replay.service:12-13), node27_timeseries_budget_preflight.py (compression.service:8).
- shell wrappers (then scanned): scheduler_file_provider_refresh_once.sh, node22_unit_failure_alert_once.sh, node27_unit_failure_alert_once.sh, node27_autopipe_cron.sh (-> node27_autopipeline.py, node27_refresh_coverage.py, node27_mvt_prewarm.py), node27_download_once.sh (-> node27_download_cycles.py), node27_raw_retention_once.sh (-> node27_raw_retention.py; also system/canonical-retention.service:31), node27_timeseries_retention_once.sh, node27_timeseries_compression_once.sh, node27_frontier_stall_alert_once.sh, node27_mvt_cache_retention_once.sh, node27_resource_governance_once.sh.
- **No unit references `production_closure`, `validate_*`, `m24_*`, `nhms-production`** (git grep: only comments at nhms-compute-compose.service:13,43 and nhms-display-compose.service:11).

### A.3 CI / Makefile
- `.github/workflows/ci.yml`: `ruff check .` (:260), `shard_tests.py` (:418), `pytest` full suite shards (:428), `check-jsonschema` loop over `schemas/` (:275-290). governance.yml: `scripts/governance/audit_repo_entropy.py` (:51,54,133). full-regression-watch.yml: `scripts/ci/full_regression_watch.py`. floating-resolve-report.yml: `scripts/ci/floating_resolve_report.py`, pytest. m15-visual-evidence.yml: frontend only. No workflow runs production_closure, validate_*, m24_*. (ci.yml path-filter lists at :69-135 only name files for change detection; they do not name any file to be deleted.)
- Makefile: `uvicorn apps.api.main:app`, `-m packages.common.migrate`, `-m db.seeds.seed_demo|seed_m1_model`, `pytest tests/`, `ruff`.
- `scripts/ci/select_ci_tests*.py` already retired (#3219; only stale .pyc remain, ignored).

### A.4 Docker-only roots (NOT production; production = systemd user units)
`infra/docker/entrypoint.sh` (console names at :36-52, `nhms-production` at :43 as forbidden command), `infra/docker/Dockerfile.app`, `infra/compose.compute.yml`, `infra/compose.display.yml`, `infra/docker-compose.dev.yml`, `infra/systemd/nhms-{compute,display}-compose.service`. The only path from these to the package is the `nhms-production` console script, and nothing invokes it. Compose still referenced by live-ish things: `scripts/governance/entropy_audit/check_topology.py:170` (scans `infra/README.two-node-docker.md`), `topology_display_env.py:367`, `tests/test_entropy_audit_topology_authority.py:299-318`, `tests/test_node22_runbook_section_markers.py`. => recommend leaving docker/compose files OUT of #3221 (not in scope; hard gate reads them).

### A.5 Explicit keeps
`apps/api/main.py` (create_app, everything it imports; no pc import: graph-verified), `scripts/node27_c4_production_acceptance.py`, `apps/api/readonly_validation_probe.py`.

### A.6 Scripts invoked from live scripts (via shell scan)
See A.2 wrappers. `scripts/ops/start-display-api.sh` (C1 runbook live path; tested by `tests/test_two_node_docker_runtime.py::test_start_display_api_*`). No live shell script calls `validate_*` or `m24_*`.

## B. `services/production_closure/` closure (45 .py files, 37,931 LOC; plus AGENTS.md 92 lines)

Live roots used: all of A.1 minus nhms-production, A.2, A.3 scripts, `apps.api.main`, C4 script, probe. Tests excluded.

### B.1 Reachable from live roots (KEEP or relocate)
| module | LOC | chain |
|---|---|---|
| `__init__` | 1 | scripts.node27_c4_production_acceptance -> pc (package init) |
| `c4_production_acceptance` | 582 | scripts.node27_c4_production_acceptance -> pc.c4_production_acceptance. Names: `bind, freeze, verify` |
| `c4_production_acceptance_io` | 779 | scripts.node27_c4_production_acceptance -> pc.c4_production_acceptance_io. Name: `C4AcceptanceError` (and imported by c4 module internally) |
| `readonly_db_validation` | 574 | apps.api.readonly_validation_probe -> pc.readonly_db_validation. Names used by probe: `RouteHttpResponse, RouteRequester, STATUS_FAIL, STATUS_PASS, _bounded_database_url, _display_app_env, _display_validation_env, _operator_headers, _response_body, _temporary_env` (probe lines 12-24; all re-exported from route_smoke/types, see readonly_db_validation.py:32-52) |
| `readonly_db_route_smoke` | 462 | probe -> readonly_db_validation -> route_smoke. Real owner of `_response_body`(:339) `_bounded_database_url`(:346) `_display_app_env`(:377) `_display_validation_env`(:387) `_display_object_store_root`(:~401) `_validation_auth_env` `_operator_headers`(:422) `_temporary_env`(:430) `_validation_pgoptions`; raises `ReadonlyDbValidationError` |
| `readonly_db_types` | 301 | -> readonly_db_validation -> types. Owner of `RouteHttpResponse`(:140), `RouteRequester`(:190), `STATUS_PASS/FAIL`(:29,31), constants `VALIDATION_*`, `DISPLAY_OBJECT_STORE_ROOT_ENVS`, and `DEFAULT_API_PROBE_ADAPTER_MODULE`(:13, the only loader of the probe) |
| `readonly_db_merge` 868, `readonly_db_permission_probes` 788, `readonly_db_probe_adapter` 816, `identity_matching` 38 | | reached only because readonly_db_validation imports them; the probe uses none of their names |

**Everything below the probe/C4 line is "live" only transitively via the probe, which itself has no live caller (finding 2).**

Relocation if variant A (package emptied except C4): the probe needs from the lane only 6 helper functions + `RouteHttpResponse` dataclass + `RouteRequester` alias + 2 status strings + `ReadonlyDbValidationError` + the 6 constants they use (route_smoke.py:339-440 ~100 LOC, types.py:29-31,140-145,190, `VALIDATION_CONNECT_TIMEOUT_SECONDS/STATEMENT/LOCK/IDLE`, `DISPLAY_OBJECT_STORE_ROOT_ENVS`). Inline them into the probe (self-contained, ~130 LOC). Alternatively delete the probe (no live caller) — owner said keep.

### B.2 Only reachable from docker-only root (`nhms-production` console script) -- DELETE
`slurm_validation` 2934 (root; only importer is itself via console script), and its fan-out: `e2e_validation` 2113, `met_validation` 2083, `ops_validation` 2912, `scale_validation` 2068, `object_store_validation` 892 + `_consumption` 316 + `_contracts` 156 + `_evidence` 139 + `_fixture` 201 + `_manifest` 364 + `_path_safety` 418 + `_runtime` 933, `readiness_validation` 1193 + `readiness_dependency_live_proofs` 381 + `_dependency_summaries` 335 + `_final_aggregation` 122 + `_item_contracts` 98 + `_live_proofs` 777 + `_scheduler_evidence` 1320 + `_scheduler_live_proof` 361 + `_scope_exclusions` 59 + `_shared_artifacts` 541. (slurm_validation `main` is the only static root; chains: slurm_validation -> X.)

### B.3 Only reachable from dormant scripts (`scripts/validate_two_node_e2e_evidence.py`) -- DELETE
`two_node_e2e_evidence` 4436 (facade; `scripts/validate_two_node_e2e_evidence.py:9`) and its lanes: `_api_lane` 380, `_browser_lane` 393, `_cross_plane_lane` 374, `_docker_preflight` 278, `_docker_security` 1607, `_final_aggregation` 441, `_logs_lane` 410, `_manual_ops_lane` 1205, `_metadata_lane` 490, `_readonly_db_lane` 1750, `_simple_live_lane` 242. (`scripts/validate_readonly_db_boundary.py:9` imports `readonly_db_validation`: C2 entry, see finding 3.)

### B.4 Unreachable from any root
None (every module is reachable from one of the three groups). AGENTS.md (92 lines) is referenced by entropy audit schema (D).

### B.5 Comment-only mentions verified by AST (no import)
- `packages/common/shud_preflight.py:8` (docstring), `workers/forcing_producer/producer.py:195,865` (docstring/comment), `services/orchestrator/scheduler_state_failure.py:1207` (comment), `services/orchestrator/scheduler_lease.py:122` (comment naming `scripts/m24_lease_nfs_proof.py`), `scripts/node27_timeseries_compression_prearm.py:131` (docstring), `services/slurm_gateway/real_backend.py:192` (comment). None are imports; leave or reword. `workers/`, `packages/`, `services/orchestrator` have zero real imports of `services.production_closure`.

## C. Reverse closure (what dies with them)

### C.1 Non-test code (verified by `rev.py`: BFS from every deleted module, then live closure recomputed with deleted nodes removed, with and without ci.yml path-filter pseudo-roots: 207 non-deleted modules are reached from the deleted set (A), 0 of them lose live reachability, 0 non-test survivors fall out of the live closure. So nothing outside the deletion list becomes dead code)
Scripts (7,139 LOC, variant A; 7,124 in B):
- `scripts/validate_two_node_docker_runtime.py` 5517 (explicit delete). Imports yaml (still used live).
- `scripts/validate_two_node_docker_source_trust.py` 575 -- **not named in the issue** but exists only for the docker lane (imports nothing from pc; cited by compose-unit comments + README.two-node-docker.md + tests). Recommend delete with the docker runtime validator; owner confirm.
- `scripts/validate_two_node_e2e_evidence.py` 15 (wrapper of two_node_e2e_evidence).
- `scripts/validate_readonly_db_boundary.py` 15 (wrapper of readonly_db_validation; DELETE in A, KEEP in B).
- `scripts/m24_emit_baseline.py` 312, `scripts/m24_gateway_proof.py` 555, `scripts/m24_lease_nfs_proof.py` 150 (m24 chain; none wired to systemd/CI; `services.m24_live` only imported by them and their tests).
- `services/m24_live/__init__.py` 33, `services/m24_live/receipt.py` 374.
- **Keep**: `scripts/validate_bugs_ledger.py` (docs ledger validator, unrelated despite `validate_` prefix).
- pyproject.toml edits: delete line 61 (`nhms-production`), 117 (`scripts/m24_gateway_proof.py` B007), 133 (`scripts/validate_two_node_docker_runtime.py` BLE001), 167-174 (8 per-file ignores for pc modules), 219 (`tests/test_two_node_e2e_evidence.py` B007). Line 208 `tests/test_production_scheduler.py` is a LIVE test: keep.
- `config/resource_profiles.yaml:42` `m24_smoke` profile: keep (used by LIVE `tests/test_production_scheduler.py:49750` and loaded by slurm gateway config).
- `infra/sbatch/run_shud_forecast_array.sbatch:60-65`: live template; the strings `production_closure_controlled_failure` / `production_closure_validation` are literal ids inside an embedded controlled-failure parse check (imports only `workers.output_parser.parser`, `packages.common.manifest_index`); no test or code asserts them (git grep: only the sbatch and `slurm_validation.py:889`). Leave (cosmetic; out of scope).

### C.2 Schemas / examples
`schemas/*.schema.json` and `schemas/examples/*`: **none is read only by the lanes**. Cross-check (grep of every schema basename): `qc_result` is referenced by pc (`e2e_validation.py`, `ops_validation.py`) but also by `packages/common/state_manager.py`, migrations, seeds -> keep. `river_identity_backfill_receipt.schema.json` has no code reference at all (pre-existing orphan; out of scope, report only). pc modules do not load any `schemas/` file (`grep schemas/ services/production_closure` empty). Delete count: 0.

### C.3 Docs
Whole-subject docs (DELETE): 
- `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md` 264; `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` 424 (the latter is read by `tests/test_two_node_e2e_evidence.py`, which is deleted with it).
- `docs/validation/production-closure.md` 751 (subject = nhms-production lanes; target of `docs/VALIDATION.md:191-217` links).
- `services/production_closure/AGENTS.md` 92.
- `infra/README.two-node-docker.md` 949 (subject = docker lane + validators; BUT scanned by `scripts/governance/entropy_audit/check_topology.py:170` and `tests/test_entropy_audit_topology_authority.py:299-318`, and by `tests/test_two_node_docker_runbook_environment_invariant.py` (643 LOC, whole file is about it), `tests/test_two_node_docker_source_trust.py:332`, `tests/test_readonly_db_validation_routes.py:617,642`). Only delete together with those; otherwise strip the validator sections.
Edit (not delete): `docs/VALIDATION.md` (671; sections 191-217 + matrix), `docs/runbooks/node-27-bringup-checklist.md` (:126, :213, :224-225 C2 entry, :319 C4 owner path if relocated), `docs/runbooks/tier-node27-timeseries-storage.md` (:832,:888,:944-946), `docs/runbooks/two-node-production-e2e-plan.md` (932; 8 hits; read by `tests/test_entropy_audit_topology_authority.py` and `tests/test_readonly_db_validation_routes.py:407,616`), `docs/runbooks/two-node-deployment-overview.md`, `docs/runbooks/qhh-continuous.md` (7), `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` (4), `docs/governance/{API_LAYER_INVERSION_INVENTORY (9), API_CONTRACT_BACKEND_CONSUMER_EVIDENCE (4), ROLE_BOUNDARY (5), STRUCTURAL_FILE_DISPOSITION_INVENTORY (8), GOVERNANCE_8_FINAL_EVIDENCE_MAP, entropy-burndown-triage (4)}.md`, `infra/env/README.md` (7), `infra/env/display.example:53`, `README.md:31,97,252,276`, `openspec/project-profile.md:9,108`, `services/orchestrator/AGENTS.md:24`. Historical (leave): `docs/review-loop-log.jsonl`, `docs/governance/entropy-audit-2026-10-16.*`, `docs/plans/*`, `openspec/changes/archive/**`.
Not affected: root `CLAUDE.md`/`AGENTS.md`, `instructions/agents/shared.md`, `scripts/AGENTS.md`, `apps/api/AGENTS.md`, `packages/common/AGENTS.md` (0 hits for any deleted name; `apps/api/AGENTS.md` only cites ROLE_BOUNDARY.md).

### C.4 Tests (variant A; LOC from files; D = tests that touch deleted code, heuristic AST)
**Delete whole file (20 files, 29,029 LOC by AST; variant-B column in F; + 3 manual adds below):**
test_identity_matching 53 (B: keep, but 1/5 tests use two_node_e2e_evidence so edit), test_m24_emit_baseline 217, test_m24_receipt 350, test_production_e2e_validation 1362, test_production_met_validation 730, test_production_ops_validation 660, test_production_readiness_validation 6715, test_production_scale_validation 1009, test_production_slurm_validation 3291, test_readonly_db_discovery_and_lane_statuses 395, test_readonly_db_discovery_integration 215, test_readonly_db_two_node_consumer 143, test_readonly_db_validation 869, test_readonly_db_validation_probes 903, test_real_readonly_db_probe_integration 61, test_real_readonly_db_route_smoke_integration 57, test_slurm_gateway_ops_auth_evidence 616, test_slurm_gateway_ops_dependency_closure 534, test_slurm_gateway_ops_dependency_hardening 893, test_two_node_e2e_evidence 9976.
Plus whole-file by manual inspection (heuristic mislabelled): test_m24_gateway_proof 455 (13/15 D; the other 2 test the same script's constants), test_two_node_docker_source_trust 420 (SCRIPT path built with Path joins; every test targets the script), test_two_node_docker_runbook_environment_invariant 643 (only if README.two-node-docker.md goes), test_object_store_validation_facade_contract 1044 (10/12 D; remaining 2 also about pc files). => whole-file total about 30.9k LOC (23 files).
**Must be SPLIT, not deleted (contain live tests):**
| file | LOC | D tests | what to preserve |
|---|---|---|---|
| test_production_object_store_validation.py | 2205 | 51/58 | 6 LocalObjectStore/safe_fs symlink/FIFO tests (`test_local_object_store_rejects_*`, `test_safe_fs_*`) exercise live `packages.common.object_store`/`safe_fs`; + 1 packager test |
| test_two_node_docker_runtime.py | 5484 | 144/170 | 26 tests: `test_start_display_api_*` (live `scripts/ops/start-display-api.sh`), `test_entrypoint_*` (docker entrypoint.sh), compose-render, gitignore |
| test_readonly_db_validation_routes.py | 817 | 14/21 | 7 secret-source/systemd-namei guard tests over runbook snippets (docs-driven) |
| test_role_boundary_static.py | 524 | 3/14 | drop `docker_runtime` import (:12) and tests at ~:214-216,:291 that assert pc paths in scan list |
| test_orchestration_chain.py | 21012 | 1/389 | drop `test_sibling_poll_interval_configs_are_untouched_by_the_orchestrator_floor` (:2731 import) |
| test_pipeline_ops_identity_envelope.py | 237 | 1/4 | drop 1 test using route_smoke privates (:11) |
| test_producer_bbox_preflight.py | 739 | 1/11 | 1 test patching `services.production_closure.met_validation._run_forcing_production` by string (:498) |
| test_river_ts_read_path_surrogate_keys.py | 989 | 2/25 | 2 tests import `scale_validation.HYDRO_MAP_PLAN_LINES_BY_STORE, QUERY_TARGETS` (:53); data would need relocation or tests dropped |
| test_safe_fs.py | 757 | 1/37 | drop `test_met_evidence_lane_reports_a_structured_evidence_code` (:293) |
| test_scheduler_evidence_decidability.py / _summary_lanes.py | 967 / 687 | 1+1 | drop the one reader test each (:451, :624); `tests/fixtures/scheduler_evidence_golden_3170.json` stays (read by summary_lanes) |
| test_status_vocab_parity.py | 758 | 1/22 | drop import :49 and `test_non_orchestrator_follow_up_sites_rebind_the_vocabulary` + any pinned pc rows |
| test_operator_action_reservation_lease.py | 601 | 1/16 | `test_the_other_lease_heartbeat_callers_are_unchanged` (:562) lists `scripts/m24_lease_nfs_proof.py`; drop that path |
| test_forcing_ts_template_census.py (767) + tests/forcing_ts_template_registry.py (740) | | 0 | registry :405,:510 and census :84,:162 count `services/production_closure/two_node_e2e_readonly_db_lane.py: 2` template statements; deleting that file requires editing the pinned census/registry (and check fixture `river_ts_templates_51f9d273.json`) |
| test_river_ts_text_identity_cleanup.py | | 0 | :1817 lists `scale_validation.py` among "unregistered" parts; harmless as a literal but the file will be missing (assert is "not in registered": passes) |
| test_list_search_contract.py | | 0 | comment only (:287) |
| test_entropy_audit_baseline_writer_summary.py:99, test_entropy_audit_instruction_inventory.py:210, tests/entropy_audit_helpers.py (BASELINE) | | | see D |
| test_node27_c4_production_acceptance.py (857) / _boundaries.py (408) | | | KEEP unchanged unless C4 modules are relocated (import paths) |
Mixed total (A): 18 files, 38,668 LOC to edit (21k of it test_orchestration_chain.py, one test).
Test helper modules importing deleted code: none by import; `tests/forcing_ts_template_registry.py` by path string only. Fixtures read only by deleted tests: none found under tests/fixtures (each fixture dir has a live reader); `tests/fixtures/river_ts_templates_51f9d273.json` has no static reader (unsure).

## D. Knock-on data files
- `tests/status_literal_allowlist.json`: remove 8 entries (lines 7, 59, 60, 61, 62, 63, 64, 65). `tests/test_status_literal_scan.py:148-150` fails on stale entries.
- `tests/import_layering_allowlist.json`: `[]` -- no change. `tests/time_helper_allowlist.json` / `time_helper_ceiling.json` ({"ceiling": 0}): 0 hits, no change.
- `tests/any_ratchet_ceiling.json`: today count 9372 vs ceiling 9500 (9372*20 >= 9500*19, passes). Using the test's own `count_any`: deleted non-test files carry **2091 Any (variant A; includes validate_two_node_docker_runtime 153, source_trust 12, m24 scripts 32 + m24_live receipt 17)** -> new count **7281**; `ratchet_verdict(7281, 9500)` FAILS ("more than 5 % below") -> set `{"ceiling": 7281}` (plus any Any added by the probe-helper relocation; recount after edit). Variant B: deleted 1902 -> 7470 -> `{"ceiling": 7470}`. Kept: c4 modules 7+9, probe 2.
- `scripts/governance/entropy_audit/line_reference_baseline.json`: 0 hits, no change.
- `.entropy-baseline/latest.json:698-720` (module `services/production_closure`, `file_count: 46`, hotspots two_node_e2e_evidence/readiness_validation), asserted by `tests/test_entropy_audit_baseline_writer_summary.py:66-100` against the tree -> regenerate via `scripts/governance/write_entropy_baseline.py` and edit its static table at `write_entropy_baseline.py:91-97`. `.entropy-baseline/2026-*.json` are dated snapshots: leave.
- `scripts/governance/entropy_audit/schema.py:240-255`: `_ScopedAgentContextConfig` for `services/production_closure` + `AGENTS.md`; `tests/test_entropy_audit_instruction_inventory.py:206-215` asserts the 4-path AGENTS set. Either keep a (shortened) AGENTS.md for the remaining C4 modules or drop the config and the test expectation together.
- `scripts/governance/entropy_audit/check_env_and_tokens.py:210` and `check_paths_and_api.py:534` (scan roots/files list `services/production_closure[/ops_validation.py]`; the latter guards with `file.exists()` so safe; the former is a root dir: verify missing dir is tolerated -- unsure); `tests/test_role_boundary_static.py:214-216,291` and `tests/test_entropy_audit_retired_paths.py:650` (tmp_path synthetic file; fine).
- `scripts/ci/full_test_durations.json`: remove entries for deleted test files (3 m24 + the rest; `scripts/ci/shard_tests.py` -- check whether stale names are tolerated; unsure).
- `docs/governance/*INVENTORY*.md` asserted by tests: only `TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` (by `tests/test_two_node_e2e_evidence.py`, deleted). `READINESS_...INVENTORY.md`, `API_LAYER_INVERSION_INVENTORY.md`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md`: no test reads them.
- openspec/specs (per-requirement grep of lane terms): whole spec about deleted lanes -> `production-closure-lane-decomposition` (3 reqs/3), `readiness-validation-lane-deepening` (4 reqs; 2 explicit hits, rest about the readiness validator), `two-node-e2e-lane-deepening` (3 reqs), `staging-end-to-end-closure` (4 reqs, all "production closure command"/m10 lane). Partly: `national-scale-performance` (4 reqs, 1-2 are the scale_validation lane, 3-4 tiles/frontend), `production-slurm-workload` (6 reqs; "Production-closure render uses the submission's own resource profile" + "Real Slurm production workload is verifiable"), `production-object-store-migration` (4; purpose cites the validators; req 4 about runtime prefix open), `orchestrator-structural-burndown` (32; "Object-store validation facade split..." 1 req), `layer-inversion-hardgate-prep` (5; "Existing readonly validation exception" 1), `forcing-production` (9; validate-met lane req), `live-meteorology-ingestion` (4; purpose only), `scoped-agent-context-governance` (3; 1 names pc AGENTS), `status-vocabulary`, `river-identity-normalization`, `real-slurm-gateway-contract`, `shud-model-package-publication`, `job-array-orchestration` (1 req each, incidental). Retire via openspec change (spec deltas REMOVED), `openspec validate --all --strict` required.

## E. Third-party dependencies (report only; uv.lock untouched)
Imports in to-be-deleted code: yaml, psycopg2, httpx, click, xarray, shapefile, ... Each also has live non-test importers (yaml 3, psycopg2 73, httpx 5, click 15, xarray 6, shapefile 6). **No pyproject dependency becomes unused.** `uv.lock` need not change.

## F. Totals (LOC)
| category | variant A | variant B |
|---|---|---|
| package `services/production_closure` .py | 36,569 (42 files) | 32,722 (35 files) |
| `services/m24_live` | 407 (2) | 407 |
| scripts (validate_*x4, m24_*x3) + source_trust | 7,139 (7 files) | 7,124 (6) |
| tests wholly deleted | ~29.0k (20 files by AST) + m24_gateway_proof 455 + source_trust 420 + facade_contract 1,044 = ~30.9k (23 files); runbook_environment_invariant 643 only if README.two-node-docker.md is also deleted | 13 files / 26,483 LOC by AST (m24, production_*, slurm_gateway_ops_*, two_node_e2e, readonly_db_two_node_consumer) + the same 3 manual adds = ~28.4k; test_readonly_db_*, test_real_readonly_*, test_identity_matching, test_pipeline_ops_identity_envelope are KEPT |
| tests edited (live tests kept) | ~39k LOC touched in 18 files (only ~30 tests removed outside the pure-lane files) | same |
| schemas | 0 | 0 |
| docs deleted | READINESS 264 + TWO_NODE 424 + validation/production-closure 751 + pc/AGENTS.md 92 = 1,531 (+949 if README.two-node-docker.md goes) | same |
| docs edited | ~25 files | ~25 |
Approx total deleted ~ 76k LOC (A: 36.6k pkg + 0.4k m24 + 7.1k scripts + 30.9k tests + 1.5k docs) and ~71k (B).

## G. Unsure items (evidence both ways)
1. **C2 tooling vs issue scope.** For: issue scope item 2 keeps only the probe + C4; probe is meaningless without the lane. Against: bringup checklist :224 and tier runbook :832 make `validate_readonly_db_boundary.py` the canonical C2 receipt generator, CLAUDE.md lists deny-write as a node-27 oracle. Needs owner choice (variant A vs B) before coding.
2. **Probe has no live caller** (loaded only by deleted lane, readonly_db_types.py:13). Keep as instructed, or delete with the lane.
3. `scripts/validate_two_node_docker_source_trust.py` (575) not on the owner's list; it is docker-lane only but compose-unit comments cite it.
4. Docker/compose/entrypoint/README.two-node-docker.md: no live user, but governance topology gate + 3 tests read them; deleting is a separate larger change.
5. Whether `check_env_and_tokens.py:210` tolerates a missing `services/production_closure` root (only matters if the package directory disappears fully; with the C4 pair staying it does not).
6. `tests/fixtures/river_ts_templates_51f9d273.json`: no static reader; census edits may interact.
7. `scripts/ci/full_test_durations.json` tolerance of stale test names.
8. Test-split classification is a name-level AST heuristic; for string-patched targets (monkeypatch "services....") I verified the listed ones by hand but did not execute pytest (forbidden).
9. Whether sbatch run ids `production_closure_*` at run_shud_forecast_array.sbatch:60-65 are asserted by a live test.
10. Option to keep the C4 pair in place (`services/production_closure/` shrinks to `__init__`, c4, c4_io) vs relocating to e.g. `scripts/` or `services/node27_acceptance/`: keeping avoids editing `tests/test_node27_c4_*`, runbooks (:319, :944-946) and entropy scoped-AGENTS config. Simplest: keep in place.

## H. Post-review addendum (checks added after first draft)

H.1 **Dynamic imports** (every `import_module`/`__import__`/`runpy`/`spec_from_file_location` call site, repo-wide): the only one that targets deleted-or-kept lane code is `services/production_closure/readonly_db_validation.py:280` `importlib.import_module(DEFAULT_API_PROBE_ADAPTER_MODULE)` with the name assembled by `".".join(("apps","api","readonly_validation_probe"))` at `readonly_db_types.py:13` (invisible to a string regex; found by hand). Other sites: `packages/common/node27_pgdata_workload_io.py:171` (caller-supplied names; no lane names), `services/precip/__init__.py:101` (static map to services.precip.*), `services/orchestrator/{chain_compat_runtime.py:75,scheduler.py:377,383,scheduler_candidate_runtime.py:247,495}` (orchestrator modules only). `scripts/validate_*`, `scripts/m24_*`, `services/m24_live` contain none.

H.2 **Retained tests importing helpers from tests that die** (graph query): `tests/test_scheduler_evidence_decidability.py:452` imports `_live_scheduler_payload` from `tests.test_production_readiness_validation` (inside the one reader test that is dropped); `tests/test_readonly_db_route_identity.py` and `tests/test_readonly_db_validation_routes.py` import 6 and 13 helpers from `tests.test_readonly_db_validation` (in variant A that module dies, so the 1 + 7 live tests in those two files must be moved/re-homed with their helpers or dropped; in variant B nothing dies there). `tests/test_slurm_gateway_ops_auth_evidence.py:24` imports from `tests.test_production_ops_validation` (both deleted). `tests/slurm_template_helpers.py` and `tests/test_safe_fs.py:290` only mention deleted tests in docstrings/comments.

H.3 **Split heuristic recheck** (taint closure over module-level helpers/bindings in the three big split files): `test_two_node_docker_runtime.py` 26 live tests, `test_production_object_store_validation.py` 7, `test_readonly_db_validation_routes.py` 7 -- none touches a deleted-bound helper (0 reclassified). Caveat: `test_every_run_docker_smoke_call_site_normalizes_tmpdir` (:4575) is an in-file meta-guard over the docker smoke call sites; after the 144 lane tests go it guards nothing, so drop it too (25 live). The `test_start_display_api_*` / `test_entrypoint_*` tests do not touch `docker_runtime`.

H.4 **Fixed bug in first-pass test classifier**: `isD()` treated any imported symbol under a kept module as deleted. After the fix variant A is unchanged; variant B changes (see F), and the C4 tests are no longer flagged.

H.5 **Basename search of deleted tests/docs/specs** (tests/, scripts/, .entropy-baseline, docs/governance, pyproject, .github, openspec/specs, instructions, AGENTS):
- `scripts/ci/full_test_durations.json`: one key per deleted/split test file (e.g. lines 162, 175-177, 268, 303-310, 328-333, 428-430, plus test_two_node_*, test_two_node_e2e_evidence). Stale keys are tolerated (`scripts/ci/shard_tests.py:86-101` uses `durations.get(path, default)`; `tests/test_ci_shard_tests.py` uses synthetic data) -> remove for hygiene only.
- Docs that cite deleted tests (edit): `docs/governance/API_CONTRACT_BACKEND_CONSUMER_EVIDENCE.md:81-85,106`, `API_CONTRACT_RETIREMENT_INVENTORY.md:122-124,172`, `API_LAYER_INVERSION_INVENTORY.md:86`, `ROLE_BOUNDARY.md:121,203,327-328`, `GOVERNANCE_8_FINAL_EVIDENCE_MAP.md:20-21`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:234-235`, `infra/env/README.md:105-124` (cites `validate_*` lines and `tests/test_two_node_docker_*` line numbers), `workers/forcing_producer/AGENTS.md:82` (verification command lists `tests/test_production_met_validation.py` -> would fail if run; edit), `openspec/specs/two-node-docker-runtime/spec.md:111,149` (names the docker runtime/source-trust tests), `openspec/specs/orchestrator-structural-burndown/spec.md:743` (names docs/validation/production-closure.md).
- No test other than the lane tests asserts on the two lane INVENTORY docs; `docs/VALIDATION.md` is read by `tests/test_basins_registry_import_db.py:6` only in a docstring.
- Structural-budget lists: no hit for the big deleted files in `scripts/governance` or `.entropy-baseline/latest.json` other than module `services/production_closure` (file_count 46, hotspots) and `write_entropy_baseline.py:91-97`.

H.4b **Missing scan root tolerated**: `scripts/governance/entropy_audit/repo_files.py:40` skips non-existent roots, so `check_env_and_tokens.py:210` is safe even if the directory vanishes. `check_paths_and_api.py:534` guards with `file.exists()`.

H.5b **sbatch roots (section A completeness)**: `infra/sbatch/*.sbatch` call only `nhms-canonical|era5|forcing|shud-runtime|parse|pipeline|state` and `workers.forcing_producer.cli`, `workers.shud_runtime.cli`, `workers.output_parser.parser`, `packages.common.{manifest_index,state_cli}`; `smoke.sbatch` runs no python. None reach production_closure.

H.6 **Limit**: roots come from checked-in units under `infra/systemd/**`, not from installed units on node-22/node-27 (no ssh). The issue's verification step (`systemctl --user cat` on both nodes) is still required to confirm no installed unit points at a deleted path.

# Installed NHMS user units (read 2026-11-19, `systemctl --user cat`)

node-22 entry points: `python -m uvicorn apps.api.main:app`; `python -m services.orchestrator.cli plan-production`;
`scripts/node22_refresh_timer_health.py`; `scripts/node22_scheduler_stall_health.py`; `scripts/node22_unit_failure_alert_once.sh`;
`scripts/node22_scheduler_evidence_retention.py`; `scripts/scheduler_file_provider_refresh_once.sh`;
`scripts/node22_scheduler_journal_retention.py`; `python -m scripts.node22_state_index_capacity_watch`;
`python -m scripts.node22_state_index_auto_prune`; `python -m services.slurm_gateway`.

node-27 entry points: display API (`nhms-display-api.service`, bash -lc ... python, sourcing `infra/env/display.env`);
`scripts/node27_autopipe_cron.sh`; `scripts/node27_coverage_freshness_alert.py`; `scripts/node27_download_once.sh`;
`scripts/node27_frontier_stall_alert_once.sh`; `scripts/node27_mvt_cache_retention_once.sh`; `scripts/node27_raw_retention_once.sh`;
`scripts/node27_resource_governance_once.sh`; `scripts/node27_timeseries_budget_preflight.py`;
`scripts/node27_timeseries_compression_once.sh`; `scripts/node27_timeseries_retention_once.sh`; `scripts/node27_unit_failure_alert_once.sh`.

No installed unit references `services/production_closure`, `scripts/validate_*`, `scripts/m24_*`, `services/m24_live` or docker/compose.
