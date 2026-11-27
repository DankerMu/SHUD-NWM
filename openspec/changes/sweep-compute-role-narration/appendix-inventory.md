# Appendix: classified inventory (snapshot of `93b352db6`, 2026-11-27)

This document is a historical snapshot of an inventory. It quotes the text the sweep replaces and
the topology gate's own vocabulary, and is not current topology.

Work list for the slices. It is a snapshot: cited line numbers drift with every merge, so a slice
re-locates each item by its text. The line classes here were assigned by reading (method in §0);
the per-PR figures come from `narration_count.py`, whose mechanical classes differ slightly
(`design.md` §3). Where this appendix and `design.md` or `tasks.md` disagree on what is done or in
which group, the design and the tasks hold: §8 below is the inventory's first cut of the slices.

Corrections found in review, 2026-11-27 (the snapshot text below is left as taken):

- The inventory searched `node-?22` only. The spellings "node 22" and "22-node" add these live lines:
  `apps/api/routes/pipeline.py` (two "Ask a node 22 operator" messages, one asserted by
  `tests/test_retry_cancel_consistency.py`); a second OpenAPI description chain
  (`apps/api/openapi_patching_pipeline.py` "…compute_control runbook on node 22" →
  `openapi/nhms.v1.yaml` → `apps/frontend/src/api/types.ts`); `infra/env/README.md` ("Compute role,
  node 22:"); the example unit `infra/systemd/nhms-slurm-gateway.service` (two comments and its
  `Description=`); the specs `hydro-result-ingest-and-publish` ("produced by node 22"),
  `display-readonly-ops-ui` ("22-node", two lines) and `multibasin-product-discovery` ("22 节点").
- §8 item 6 says the files under `infra/systemd/node22/` have no narration. Two do:
  `nhms-compute-scheduler.service` and `nhms-slurm-gateway.service` end their `Description=` with
  "(node 22 user)". With the 14 found, 16 manifest units carry the codename in `Description=`.
- §3 says no test pins the text of `apps/api/routes/pipeline.py`. One does (above).
- A third heading is a link target: `production-ops/recalibration-and-archive.md` §5.8, linked from
  `current-production-ops.md`.
- Hyphenated compounds are narration, not names: `node-22-local` (6 lines), `node-22-operator` (2),
  `node-22-only` (1), `node-22/operator` (1).
- Other unlisted lines: `config/calibration_overrides.yaml` ("Measured on node-22", undated);
  `docs/VALIDATION.md` (host alias of the retired host).

Scope: `git grep -iE 'node-?22'` minus the six history paths = 390 files / 2507 lines (matches the baseline).
Non-ASCII file names (docs/ForcingReplace/..., docs/forcing数据处理...) need `core.quotepath=false`; without it a tracked-file walk reports 388/2500.

## 0. Method and error bars

Line-level classifier, each hit line gets exactly one class. Per occurrence: `node22` / `NODE22` glued to word characters, `_`, `.`, `/`, `@`, `#`, `-x` is an identifier token; a free-standing `node-22` (also `node-22's`, `node-22/node-27`) is narration; free-standing `node22` is ambiguous (10 tests, 6 scripts, 2 services lines). Python files use `tokenize` + `ast` (comment / docstring / string literal / name); other files by extension.

Line precedence: persisted-value > gate-vocabulary > human-text-in-code / test-text / prose > identifier. A line is `identifier` only if every occurrence is identifier-shaped; 35 lines mix identifier and narration and count as narration.

Syntactic rules:
- Python string that is exactly `node-22` / `node22:55433` / `210.77.77.22` (code) -> persisted-value (forbidden-host sets, receipt `source`).
- Any other Python string with spaces -> human-text-in-code (tests: test-text).
- `scripts/governance/entropy_audit/topology_{context_rules,predicates}.py` -> gate-vocabulary (match tokens, behaviour not prose).
- schemas/ JSON keys `node22`, `node22_touched` -> persisted-value.
- `echo node-22` / mail Subject in `.sh` -> persisted-value (emitted).
- `.md`, `.service`, `.timer`, `.example`, spec -> prose.

Hand check: 120 random lines (20 from each of tests-identifier, scripts-identifier, runbooks-identifier, specs-identifier, runbooks-prose, tests-test-text): 0 misfiled. Estimated error under 2% for large cells. Weak spots:
- the 35 mixed lines;
- fenced code in md is counted as `prose` (35 ssh lines to the old host live there);
- multi-line string fixtures in tests are attributed only coarsely;
- `identifier` also contains 5 stale-looking link texts inside prose (`...manual-recovery.md` links), which stay.

Areas: `instructions` = root CLAUDE/AGENTS, `instructions/agents/`, and the 6 scoped `AGENTS.md`. `other` = progress.md, config/, openapi/, pyproject.toml, openspec/glossary.md, project-profile.md, openspec/changes/(active).

## 1. Classification (lines)

Reconciliation with the baseline per-area figures: the baseline counts scoped `AGENTS.md` under their directory; here they sit in `instructions`: scripts 409 + 4 = 413, services 73 + 4 = 77, apps 17 + 2 = 19. Total 2507.

| area | identifier | persisted-value | gate-vocab | human-text-in-code | test-text | prose | total |
|---|---|---|---|---|---|---|---|
| docs/runbooks | 172 | 0 | 0 | 0 | 0 | 312 | 484 |
| other docs (docs/*, adr, spec, governance, modules) | 7 | 0 | 0 | 0 | 0 | 53 | 60 |
| openspec/specs | 36 | 0 | 0 | 0 | 0 | 195 | 231 |
| instructions | 5 | 0 | 0 | 0 | 0 | 14 | 19 |
| apps | 9 | 0 | 0 | 5 | 3 | 0 | 17 |
| packages | 0 | 0 | 0 | 17 | 0 | 0 | 17 |
| services | 34 | 3 | 0 | 36 | 0 | 0 | 73 |
| workers | 0 | 0 | 0 | 4 | 0 | 0 | 4 |
| scripts | 277 | 6 | 29 | 95 | 0 | 2 | 409 |
| infra | 62 | 0 | 0 | 0 | 0 | 73 | 135 |
| tests | 656 | 0 | 0 | 0 | 364 | 0 | 1020 |
| schemas | 0 | 6 | 0 | 1 | 0 | 0 | 7 |
| db | 0 | 0 | 0 | 3 | 0 | 0 | 3 |
| other | 6 | 0 | 0 | 0 | 0 | 22 | 28 |
| TOTAL | 1264 | 15 | 29 | 161 | 367 | 671 | 2507 |

Meaning: 1264 identifier lines stay; 15 persisted-value + 29 gate-vocab cannot be reworded without a behaviour change; about 1199 lines (161 + 367 + 671) are narration candidates, of which many are frozen by pins (items 2 and 3).

Breakdowns:
- **tests/test-text 367 lines**: 198 plain comment/docstring/test-name (reword freely); 5 exact-literal asserts (`"node-22"` in required-term sets, negative pins); 10 pin-on-prose; 1 pin-on-code-text; 14 string-fixtures found only in history docs; about 139 synthetic fixtures / own-output strings (gate input texts, failure messages). Of the synthetic ones, about 100 are gate input fixtures in `tests/test_entropy_audit_*` that must keep `node-22`.
- **persisted-value 15**:
  - `schemas/timeseries_compression_live_evidence.schema.json:214,217,302,307`
  - `schemas/examples/timeseries_compression_live_evidence.example.json:1613,1667`
  - `scripts/node27_timeseries_compression_live_evidence.py:252` and `scripts/node27_timeseries_compression_supervisor.py:126` (JSON key `node22` / `node22_touched`, written into evidence)
  - `scripts/node27_autopipeline.py:227-228`, `services/orchestrator/file_orchestration_migration.py:62-63` (forbidden-host sets; `node27_download_cycles.py:198` holds `210.77.77.22` without matching the node-22 regex)
  - `services/orchestrator/file_orchestration_migration.py:1337` (receipt `source` default `"node22:55433"`, asserted at `tests/test_file_orchestration_migration.py:189`)
  - `scripts/node22_unit_failure_alert_once.sh:28` (`HOST` fallback `node-22`) and `:97` (mail Subject `[NHMS node-22] unit failed:`)
- **human-text-in-code 161**: scripts 95 (comments 22, docstrings 45, strings/help 27+1), services 36, packages 17, apps 5, workers 4, db 3 (SQL comments plus `COMMENT ON` text), schemas 1.
- **infra prose 73**: unit `Description=` 14 lines in 14 hashed units; the rest are `#` comments in units and env examples, plus `infra/env/README.md` 25 prose lines.

## 2. Guards that pin prose

### 2a. Topology hard gate (the real-repo test and CI)

- `tests/test_entropy_audit_report_contract.py:208` (`current_repo_hard_gate_has_zero_production_topology_findings`) runs `build_report(REPO_ROOT, mode="hard-gate")` and requires `hard_gate_status == "pass"`. CI job `Production Topology Hard Gate` (`.github/workflows/governance.yml:112`) runs the same. Any sweep PR must stay gate-clean.
- Scanned set: `scripts/`, `infra/env`, `infra/systemd`, `instructions/agents`, `docs/governance`, `docs/runbooks`, `openspec/changes`, `openspec/specs`, `AGENTS.md`, `CLAUDE.md`, `openspec/project-profile.md` (`check_topology.py:181-197`). Not scanned: `docs/adr`, `docs/spec`, `docs/modules`, `docs/*.md` other than governance, `progress.md`, `apps/`, `tests/`.
- Skip rule `_topology_document_is_non_current` (`check_topology.py:234-265`, `:383-428`) has two exemptions: (a) a complete whole-document archive-status front matter; (b) a prose declaration in the first 80 lines that has a whole-document subject (`this document|this runbook|本文|本文件|本 runbook`...), a non-current term (`not current`, `不是当前`, `当前部署事实不同`...) and a historical term (`historical|设计意图|superseded|归档|历史`...), unless the top 80 lines carry one of the two current-ops title phrases the function names. Five paths are never skipped: `AGENTS.md`, `CLAUDE.md`, `docs/governance/ROLE_BOUNDARY.md`, `docs/runbooks/current-production-ops.md`, `openspec/project-profile.md`; a file named as `current_authority` by another complete marker is never skipped (currently `docs/runbooks/current-production-ops.md`, `scripts/diagnostic/qhh/README.md`, `openspec/specs/mvt-tile-contract/spec.md`, `services/tiles/mvt.py`).
- Measured by calling `_topology_document_is_non_current` on the working tree (read-only): of the docs with narration that are in the scan set, 111 are scanned and 8 are skipped: `docs/runbooks/forcing-copyback-backfill.md`, `qhh-22-business-bringup.md`, `qhh-backend-smoke.md`, `qhh-continuous.md`, `qhh-mvp-production-like-e2e-checklist.md`, `qhh-mvp-smoke-evidence.md`, `two-node-deployment-overview.md`, `two-node-production-e2e-plan.md`. `node22-db-retirement-runbook.md` is scanned. Not in the scan set: docs/adr, docs/spec, docs/modules, docs/ForcingReplace, docs/forcing..., progress.md, openapi, config, apps/**/AGENTS.md, services/*/AGENTS.md, openspec/glossary.md.
- Subject recognition after #3358 (`topology_predicates.py`):
  - `_topology_mentions_node22` (:188) matches `\bnode[-_ ]?22\b` and the bare `22` shorthand (`22 is|as|owns|host|node|节点|写入|postgres|db|数据库`, line-start or after `|`/`:`).
  - `_TOPOLOGY_COMPUTE_HOST` (:23-26) matches `w3` / `w2` (also `nwm-w3`) and runs of them (`w3/w2`, `w3 / w2`, `w3、w2`, `w3 和 w2`, `and`/`or`); `_topology_rewrite_compute_hosts` (:63-71) rewrites each run to `node-22` before the DB-writer predicates run (`check_topology.py:63`).
  - Local-PG leg: `_topology_line_has_compute_host_local_postgres_drift` (:74-91) is a separate clause check on `w3`/`w2` + `local postgres|local pg|本地 pg|本机 pg...` without a prohibition token.
  - Fact: the strings "compute role", "计算角色", "计算节点", "计算控制面" are **not** recognised as a subject anywhere in `scripts/governance/`. "compute role" occurs only inside finding texts (`check_topology.py:87-135`). `topology_context_rules.py:66-68` has `node-22 checkout`, `node22 checkout`, `node-22 compute`, `计算控制面` as DATABASE_URL-context tokens.
  - Consequence: a rewritten sentence such as "the compute role is the active DB writer" is invisible to the gate; the gate only covers node-22, bare 22, w3, w2.
- Gate match vocabulary containing node-22 (behaviour): `topology_context_rules.py:66,67,68,273-276,281-283,295-296,338,442-443,457-460,708`; `topology_predicates.py:63,317-322,341,351`. Hit counts: 29 lines in 2 files; docstrings/comments in the same files: 6.
- Finding texts (human-text, emitted into JSON reports): `check_topology.py:10,88,91,95,125,130,134,135` (8 lines; titles/descriptions/recommendations naming `compute role (node-22 / w3 / w2)`). `git grep 'compute role' -- tests` returns only comments (`test_entropy_audit_topology_db_boundary.py:837-839`, `test_node22_unit_tracking.py:16`, `test_compute_system_units_tracking.py:4`), so no test asserts these texts; `check_id`s `production-topology-node22-db-writer` and `production-topology-node22-local-postgres` are identifiers and appear in 40+ test lines and in `openspec/specs/node22-*` and `production-topology-contract`.
- Fixture tests: `tests/test_entropy_audit_topology_authority.py` (102 lines), `tests/test_entropy_audit_topology_db_boundary.py` (110), `tests/test_entropy_audit_retired_paths.py`, `tests/test_entropy_audit_scan_boundaries.py`. All write tmp repos; node-22 sentences are the gate's input and must stay. They are not pins on repo docs, except `test_entropy_audit_topology_authority.py:40` (a fixture copied from `recalibration-and-archive.md`).
- Other real-repo audit tests that read the working tree: `tests/test_entropy_audit_route_authority_caching.py:243,479` (no unallowlisted findings in current docs); `tests/test_entropy_audit_line_references.py:237`. The line-reference gate counts `file.py:NNN` and backtick `` `:NNN `` in Python comments/docstrings against `line_reference_baseline.json`; reworded comments must not add such a token (`:55433` in backticks would count).

### 2b. Tests that assert on a doc / spec / instruction sentence or token containing node-22

AST string-constant extraction over `tests/` + frontend tests, each constant searched verbatim in tracked docs.

| test (file:line) | pinned string | pinned doc |
|---|---|---|
| tests/test_node22_runbook_section_markers.py:17,20,40 | heading `### 5.1 22 节点部署入口`; sentence "`infra/env/compute.env` 是已退役的 docker-e2e 产物，node-22 没有任何单元加载它；...[README](../../infra/env/README.md#node-22-unit--environmentfile-authority-1694)" (exact 3-line equality) | docs/runbooks/two-node-deployment-overview.md:205-207 (+ README anchor) |
| tests/test_node22_unit_failure_alert.py:42,138,152 and tests/test_node22_unit_tracking.py:43,167 | heading `## node-22 unit → EnvironmentFile authority (#1694)` (also an anchor target; the two tests parse the table beneath it) | infra/env/README.md:39 |
| tests/test_node22_entrypoint_invariant_python_scan.py:243 | `- \`python -m services.slurm_gateway\` 在 node-22 运行。` (historical original, verbatim) | docs/runbooks/production-ops/gateway-and-services.md:517 |
| same:247-255 | the first line containing `node-22 compute` and `services.slurm_gateway` must carry the exact venv python | docs/runbooks/production-ops/overview.md:41 (table row `\| node-22 compute \| ...`) |
| tests/test_node22_entrypoint_invariant.py:544-552 | required terms `node-22`, `3.12.7`, `#1831`, `不要`, `` `uv` `` on the retired-host line; `node-22 例外` must be absent | CLAUDE.md:83, AGENTS.md:83, instructions/agents/shared.md:77 (also the compute-role line `**计算节点（w3 / w2）例外**` at :73 and the byte-exact generated-roots test :493-509) |
| tests/test_node22_entrypoint_invariant.py:438 | negative: `"node-22" not in text` | tests/conftest.py skip guidance |
| tests/test_node22_entrypoint_invariant.py:144-170,196-215,557-614 | command scans over production-ops/*.md, failed-basin-retry.md, ci-test-routing.md, forcing-copyback-backfill.md ("historical" or "归档" must appear), qhh-22-business-bringup.md front matter keys | not node-22 text itself; a rewrite must keep the `uv run` / exact-venv lines |
| tests/test_role_boundary_static.py:411 | required-term set contains `node-22` | docs/governance/ROLE_BOUNDARY.md |
| tests/test_role_boundary_static.py:465 | "node-22 runs the Slurm/SHUD compute wrapper" | ROLE_BOUNDARY.md:27 |
| tests/test_role_boundary_static.py:466 | "node-22 currently\nhosts compute/Slurm/artifact production only" (line-wrap sensitive) | ROLE_BOUNDARY.md |
| tests/test_role_boundary_static.py:467 | "node-22 是纯计算 / Slurm / SHUD / artifact producer" | docs/runbooks/two-node-deployment-overview.md:19 (the 2026-06-22 status note, not the #3360 banner; the doc is gate-skipped) |
| tests/test_role_boundary_static.py:468 | "Slurm scheduling/runtime receipts run on node-22 only when sbatch" | openspec/project-profile.md:58 |
| apps/frontend/.../JobsTable.test.tsx:239,276; StageCard.test.tsx:126 | `toHaveTextContent('22 compute-control')` | UI string, see item 3 |
| tests/test_file_orchestration_migration.py:1829 | `match="node-22"` | code message, item 3 |

Mechanical cross-check (read-only): every single-line string constant of at least 20 characters with a space in `tests/` and the frontend tests (11849 constants, docstrings excluded, first line of multi-line constants) was searched inside every doc/spec/instruction line classified as node-22 narration. Matches: exactly the rows above, plus `tests/test_entropy_audit_topology_authority.py:40` (a fixture sentence equal to `recalibration-and-archive.md:19`), `tests/test_entropy_audit_topology_db_boundary.py:739` (fixture equal to the title of node22-db-retirement-runbook.md:1) and `AccountingStoreFlags = (null)` (not narration; it matches `failed-basin-retry.md:334`). Limits: constants shorter than 20 characters, without a space, or split over several literals are not covered; `tests/test_production_scheduler.py` (73 doc/path references) reads `docs/VALIDATION.md`, `qhh-continuous.md` and `qhh-mvp-production-like-e2e-checklist.md` by section markers (`:35638-35660`, no node-22 text) and `infra/env/compute.scheduler-dbfree.env.example` for one line (`:26068`).

Docs and templates that tests read by structure (heading, fence, marker, table), where node-22 narration shares the file: `file-provider-refresh.md` (`tests/test_node22_refresh_timer_health_runbook_and_env.py`), `stuck-detection.md` (`tests/test_node22_scheduler_stall_health.py:70`), `known-issues-state-index.md` (`tests/test_node22_state_index_auto_prune.py:1018`), `infra/env/README.md` (`tests/test_node22_scheduler_env_drift_table.py:31`, `tests/test_env_templates.py`), `node22-control-plane-manual-recovery.md` (`tests/test_operator_reentry_confirmation.py:536` HTML comment markers `reentry-refusal-reasons`; `tests/test_monitoring_api.py:2259` slug file must exist), `gateway-and-services.md` (`tests/test_slurm_gateway_deployment_contract.py`), `two-node-production-e2e-plan.md` (`tests/test_readonly_db_validation_routes.py:407,616`), `scripts/diagnostic/qhh/README.md` and `qhh-backend-smoke.md` (`tests/test_qhh_scripts_static.py:636-702`), `job-retry-mechanism/spec.md` (`tests/test_retry.py:37-90`). I did not read all of those assertions; whether a narration line inside them is free to reword is unverified.

Anchors derived from node-22 headings (rename breaks links): `known-issues-pipeline.md#85-node-22-scheduler-stuck-after-missing-forcing-artifact` x5 (current-production-ops.md:250, node22-control-plane-manual-recovery.md:441, file-provider-refresh.md:1004, gateway-and-services.md:169, stuck-detection.md:232); `infra/env/README.md#node-22-unit--environmentfile-authority-1694` x3 (two-node-deployment-overview.md:207,239; the marker test).

No test pins the OpenSpec specs' prose (only `job-retry-mechanism` scenario header, and the real-repo topology gate scanning `openspec/specs`).

## 3. Human text in code whose exact text a test pins

| text (file:line) | pinned by |
|---|---|
| scripts/basin_retirement/run.py:72-73 "the node-22 finish receipt ... has not finished on node-22." | tests/test_node27_retire_basin_dry_run.py:113 (`"has not finished on node-22" in refused[0]`); tests/test_node27_retire_basin_refusals.py:83 |
| services/orchestrator/file_orchestration_migration.py:1806,1815 ("historical node-22 export requires --allow-historical-node22"; "...must target the node-22 historical PostgreSQL host") | tests/test_file_orchestration_migration.py:1829 `match="node-22"` (regex hits either) |
| scripts/node22_unit_failure_alert_once.sh:97 Subject `[NHMS node-22] unit failed: $SAFE_UNIT`, :28 HOST fallback `node-22`, :102 body "entered the failed state on $HOST at" | tests/test_node22_unit_failure_alert.py:249, :255, :292 (the test depends on the `node-22` fallback value); `SENDER = "NHMS Node-22 Alert <alerts@example.invalid>"` (:71) is the test's own input env value and its own expected output, not a pin on repo text |
| apps/frontend/src/components/monitoring/JobsTable.tsx:92-93, BasinFailures.tsx:32-33 ("22 compute-control 节点") | JobsTable.test.tsx:239,276; StageCard.test.tsx:126; apps/frontend/src/lib/c4DisplayEvidence/dom.ts:58 (production live-evidence probe `includes('需要恢复时在 22 compute-control')`, runs against the real page); apps/frontend/src/test/c4DisplayFakePage.ts:211 (fake page string); openspec/specs/display-readonly-ops-ui/spec.md:100 |
| apps/api/openapi_patching_pipeline.py:213-215 "...node-22 manual-repair marker lane (scripts/node22_manual_retry_failed_runs.py...)" | generated chain, checked in verbatim: openapi/nhms.v1.yaml:1697-1698 -> apps/frontend/src/api/types.ts:463; CI `check:api-types` (`apps/frontend/package.json:23`) diffs it; `tests/test_api_contract*.py` reference the yaml (equality not verified here). `tests/test_response_model_schema_parity.py:245` repeats the sentence as its own dict value (not compared with the source) |
| apps/api/routes/pipeline.py:205 `message="Control-plane retry is a manual node-22 action;..."` | no test or snapshot pins it (searched tests, openapi, frontend) |
| scripts/node27_autopipeline.py:454,2635; scripts/node27_download_cycles.py:203; services/orchestrator/scheduler_preflight.py:138 | no pin found by verbatim-fragment search |
| scripts/governance/entropy_audit/check_topology.py finding titles | no test pins them |
| db/migrations/000035_qhh_display_coverage_materialization.sql:45 `COMMENT ON ... 'optional and node-27-local (node-22 keeps the CTE path).'` | no test; the text is persisted into `pg_description` on fresh databases (applied databases keep the old text). The migration runner has no content checksum (`packages/common/migrate.py` records only the version table) |

## 4. Docs triage

Counts = narration lines (identifier lines in parentheses). Judgement: live = describes current operation; mixed; historical-only.
Banner column: #3360 = the `计算角色主机说明（2026-11-23 起）` blockquote ("node-22" here means the compute role, hosted on w3 (control) + w2; host `210.77.77.22` retired, rollback archive only; literal ssh to `210.77.77.22` must be run on w3; link to w2-w3-compute-topology.md). `ptr` = the shorter 4-line pointer ("本页的 'node-22' 指计算角色，现由 w3 / w2 承载；字面的 `ssh … 210.77.77.22` 命令一律登录 w3 后执行，不得在原主机上执行"). The first-commit file list was `319aebb46`, `e03da1b5c`, `5c9685ac4` (tagged #3345; the PR number #3360 itself was not verified in git log).

| file | narr (id) | judgement | banner / pre-existing marker |
|---|---|---|---|
| docs/runbooks/production-ops/artifacts-and-storage.md | 40 (0) | live; 8 `ssh ...@210.77.77.22` command lines (229,310,339,399,468,501,688,840) | ptr :4 |
| docs/runbooks/production-ops/service-bringup.md | 31 (15) | mixed: live bring-up plus dated 2026-06-30 / 2026-08-22 field notes (:326, :364-530); 3 ssh command lines (196,271,330) | ptr :7 + re-bring-up note :11-14 |
| docs/runbooks/production-ops/known-issues-state-index.md | 24 (14) | live; 7 ssh command lines (21,60,194,329,593,712,810) | ptr :4 |
| infra/env/README.md | 25 (12) | live (unit-to-env authority; heading is a test pin) | none |
| docs/runbooks/production-ops/recalibration-and-archive.md | 21 (12) | live; 1 ssh command line (:29) | ptr :4 |
| docs/runbooks/production-ops/gateway-and-services.md | 15 (17) | live; 2 ssh command lines (124,829) | ptr :4 |
| docs/runbooks/production-ops/file-provider-refresh.md | 14 (19) | live; 1 ssh command line (:982) | ptr :4 |
| docs/runbooks/production-ops/stuck-detection.md | 11 (24) | live; 9 ssh command lines (33,104,167,653,656,661,670,673,754) | ptr :4 |
| docs/runbooks/node22-control-plane-manual-recovery.md | 11 (19) | live | full #3360 banner + rollback note :1-14 |
| docs/runbooks/production-ops/operating-scope.md | 15 (5) | mixed: current-state rules plus dated retirement logs (§7.1-7.4) | none |
| docs/runbooks/production-ops/known-issues-pipeline.md | 9 (14) | live (§8.5) | none |
| docs/runbooks/production-ops/overview.md | 6 (0) | live (§1 current conclusion, §2 nodes) | none |
| docs/runbooks/current-production-ops.md | 5 (2) | live (title scope sentence still says "node-22（计算角色，现由 w3 / w2 承载）") | full #3360 banner :7-14 |
| docs/runbooks/two-node-deployment-overview.md | 7 (1) + 19 lines of bare `22 节点`/`22 侧` shorthand | mixed: M22 design-intent background | #3360 banner :8-16; gate-skipped by the prose rule ("不是当前 host 分配操作手册"); the `:205 ### 5.1 22 节点部署入口` heading is a test pin |
| docs/runbooks/failed-basin-retry.md | 8 (5) | live | none |
| docs/runbooks/scheduler-dbfree-typed-reasons.md | 6 (6) | live | none |
| docs/runbooks/slurm-gateway-preflight.md | 4 (0) | live (title "Node-22 Slurm Gateway ...") | none |
| docs/runbooks/canonical-precip-mirror-backfill.md | 5 (2) +1 shorthand | live | none |
| docs/runbooks/qhh-controlled-failure-retry-evidence.md | 4 (1) | mixed | none |
| docs/runbooks/node-27-bringup-checklist.md | 4 (0) +1 shorthand | mixed (node-27 oriented) | none |
| docs/runbooks/display-readonly-live-mvt.md | 4 (0) | mixed: historical receipt context; carries `210.77.77.22:55433` as "do not copy" | none |
| docs/runbooks/tier-node27-timeseries-storage.md | 12 (0) | mixed: node-27 storage, node-22 incidental (:874,1095,2913,4376...) | none |
| docs/runbooks/node22-db-retirement-runbook.md | 9 (3) | historical-only (completed 2026-06-29 DB retirement cutover, `:55433`) | none ("Current Status" section states it is stopped) |
| docs/runbooks/qhh-22-business-bringup.md | 14 (2) | historical-only (bring-up of the old host, docker lane `nhms-22-e2e-db`, `xnode`, `10.0.2.100:55433`; 1 ssh command line :205) | gate-skipped. Pre-existing front matter: `status: historical baseline`, `status_since: 2026-08-24`, `archive_scope: whole-document`, `current_authority`, `retained_for` (pinned by `test_qhh_bringup_has_historical_marker`) |
| docs/runbooks/qhh-continuous.md | 6 (1) | historical-only | gate-skipped. Pre-existing front matter `status: superseded`, `archive_scope: whole-document` |
| docs/runbooks/forcing-copyback-backfill.md | 12 (0) | historical-only (pre-#837), except :105-109 (live flock interop fact) | gate-skipped by the prose declaration rule (`check_topology.py:383-428`); pre-existing blockquote "Status: historical, not current production runbook" (no front matter); `test_node22_entrypoint_invariant.py:621` requires "historical" or "归档" |
| docs/runbooks/two-node-production-e2e-plan.md | 3 (1) + 14 shorthand | historical-only (M22 E2E evidence plan) | gate-skipped (prose declaration); pre-existing blockquote "2026-06-22 status: historical / superseded M22 evidence plan" |
| docs/runbooks/qhh-mvp-production-like-e2e-checklist.md | 4 (5) | historical-only | gate-skipped (prose declaration); pre-existing "历史 / 已被 M26 取代" blockquote |
| docs/runbooks/production-ops/frontier-stall-alert.md | 2 (0) | live (:12,:42) | none |
| docs/runbooks/production-ops/parse-failure-residency-alert.md | 1 (0) | live (:41) | none |
| docs/runbooks/held-forcing-absence-exit.md | 1 (0) | live (:91 "On node-22 use the exact interpreter") | none |
| docs/runbooks/ci-test-routing.md | 1 (0) | live (:120); governed by `test_node22_entrypoint_invariant.py:249-260,333-416` (no `uv sync`, no `cd /scratch/frd_muziyao/NWM`) | none |
| docs/runbooks/qhh-backend-smoke.md | 2 (0) | historical evidence with diagnostic-only recipe (:10,:39); gate-skipped (prose rule) | pre-existing "历史证据说明" + "执行边界（DIAGNOSTIC-ONLY）" blockquote; read by `test_qhh_scripts_static.py:702` |
| docs/runbooks/qhh-mvp-smoke-evidence.md | 1 (1) | historical-only; gate-skipped | pre-existing "Historical / superseded by M26" blockquote |
| docs/runbooks/w2-w3-compute-topology.md | 1 (3) | live (new) | n/a |
| docs/governance/ROLE_BOUNDARY.md | 10 (0) | live (:18-30 already says node-22 = the compute role on w3/w2; later lines still "node-22") | #3360 paragraph :18-26 |
| openspec/project-profile.md | 2 (0) | live (:58 pinned by role_boundary test) | none |
| openspec/glossary.md | 1 (0) | live (:36 "node-22 scheduler registry manifest") | none |
| docs/adr/0008, 0002, 0005, 0007, 0003, 0009 | 12+6+1+2+2+1 | decision records, historical by nature; 0008 text is host-specific ("按主机选择") | none |
| docs/spec/03_database_design.md (4), 04_api_design.md (1+1 id), 06_frontend_gis_design.md (1) | 6 | live design docs | none |
| docs/modules/02_*, 05_* | 3 | live design docs | none |
| docs/ForcingReplace/CMFD...md (5), docs/forcing数据处理流程与rSHUD一致性说明.md (2) | 7 | mixed (2026-07-18 scale statements "node-22 共有 18 个业务流域") | none |
| docs/governance/LEGACY_DEAD_CODE_INVENTORY.md (2), GOVERNANCE_8_FINAL_EVIDENCE_MAP.md (1) | 3 | historical-only | none |
| progress.md | 7 (0) | historical-only (log of M22-M24; "QHH node-22 业务化（live，进行中）") | none |
| scripts/diagnostic/qhh/README.md | 2 | live ("canonical active node-22 checkout") ; pinned by `test_qhh_scripts_static.py:636-677` | none |
| infra/env/*.example (6 files: compute.scheduler-dbfree 10, node22-unit-failure-alert 5, provider-refresh 1, display 1, node27-download 1, node27-raw-retention 2) | 20 | live (comments) | none |

Basis of the judgements: file title, first 15 lines, front matter, heading map of the hit lines and the hit lines themselves; no document was read end to end. The rows are checked against Appendix A (every docs file with counts; narration column sums to 312 for runbooks and 53 for other docs).

Banner-carrying files from #3360 = 11: current-production-ops, node22-control-plane-manual-recovery, two-node-deployment-overview (full banner); 7 production-ops pages (artifacts-and-storage, file-provider-refresh, gateway-and-services, known-issues-state-index, recalibration-and-archive, service-bringup, stuck-detection) with the pointer; ROLE_BOUNDARY paragraph. Gate-wise, `node22-db-retirement-runbook.md` is the only historical-only runbook that is still scanned (the spec `node22-db-retirement-cutover` and `production-topology-contract` still describe its guardrails).

## 5. Specs triage (openspec/specs, 51 specs, 231 lines: 195 narration / 36 identifier)

Counts n = narration lines, i = identifier lines; the complete per-spec table with requirement-to-line maps is Appendix B. Spec names containing node22 (6): node22-db-retirement-cutover, node22-dbfree-runtime, node22-env-hygiene, node22-unit-failure-alert, node22-unit-tracking, slurm-gateway-node22-deployment. Requirement/Scenario headings with `node-22`: 18 requirement headings and 11 scenario headings, e.g. `compute-scheduler-operationalization/spec.md:45,63`, `node22-db-retirement-cutover/spec.md:8,58,64`, `node22-dbfree-runtime:8`, `node22-unit-failure-alert:8,27,40`, `node22-unit-tracking:8`, `production-topology-contract:41,78,117,169,184,209,236`, `production-ops-readiness:187,260`, `production-scheduler-orchestration:30,38`, `python-environment-truth:26`, `real-shud-slurm-execution:41`, `slurm-gateway-node22-deployment:9,52`, `forcing-copyback-backfill:66`, `forcing-domain-handoff:101`, `basin-retirement:27`, `node27-download-orchestration:47`. Changing a requirement header needs a RENAMED delta; scenario titles are free.

| spec | n | i | notes |
|---|---|---|---|
| production-topology-contract | 29 | 1 | states node-22 = compute role present tense; requires the banner (:36-48), "Runbooks that still say node-22 carry a banner" (:41), a scenario that `node-22 there denotes the retired host` (:75); mandates links to w2-w3-compute-topology.md. Needs delta (#3360 vintage). |
| production-ops-readiness | 11 | 0 | :260-287 "node-22 remains compute and Slurm Gateway only" and the **banner requirement** (:277-287: banner text, `210.77.77.22` pointer on each production-ops page). Needs delta; a sweep that removes banners contradicts it. |
| node22-db-retirement-cutover | 11 | 2 | historical event spec (completed 2026-06-29); present tense "SHALL stop node-22 historical PostgreSQL" |
| node22-unit-failure-alert | 6 | 8 | mostly names; :4,8,9,27,37,40 "node-22 SHALL reach..." present tense |
| production-scheduler-orchestration | 9 | 2 | :13,30,32,38,40,59 "business-runnable on node-22" present tense; :1090-1096 |
| slurm-gateway-node22-deployment | 9 | 1 | "A standalone Slurm gateway service is deployed on node-22" (:9) present-tense location |
| compute-scheduler-operationalization | 7 | 1 | :45,46,61,63 |
| scheduler-registry-refresh | 8 | 0 | lines 508-1231 |
| job-retry-mechanism | 7 | 0 | :5, 2572, 2601, 2696-2805 |
| node22-dbfree-runtime | 6 | 1 | :8,10,33,69,77,91 present tense |
| node27-download-orchestration | 7 | 0 | "complete before node-22 scheduling" |
| node22-unit-tracking | 4 | 5 | #3360 already edited (compute role / control host wording) |
| scheduler-stall-alerting 5/2, variant-activation-cutover 6/1, canonical-precip-copyback 6/0, basin-retirement 4/0, forcing-domain-handoff 4/0, pipeline-job-persistence 4/0 | | | present-tense location in prose |
| remaining 36 specs | 1-3 each | | single mentions; python-environment-truth (2/0) was edited by #3360 |
| display-readonly-ops-ui | 0 | 0 | but :100 mandates "22 compute-control recovery runbook" guidance (shorthand, counted in item 7) |

Specs that only use node-22 inside names/paths (no narration): entropy-automation (0/4), compute-system-units-tracking (0/1).
Specs touched by archive `2026-11-23-record-compute-role-on-w3-w2`: node22-unit-tracking, production-ops-readiness, production-topology-contract, python-environment-truth (archive dir has proposal.md, tasks.md, specs/ for these four).
Active (non-archived) change in the tree: `openspec/changes/fix-node22-keliya-forecast-output-recompute/` (proposal, design, tasks, one spec delta), 14 lines, 13 narration; tasks.md:20 states the change is superseded (basins_keliya_shud no longer in production). Scanned by the hard gate (`openspec/changes`).

## 6. Terms in use today (updated files)

Counts in: shared.md / CLAUDE.md (identical, generated) / w2-w3-compute-topology.md / glossary / production-topology-contract / production-ops-readiness / node22-unit-tracking / python-environment-truth / ROLE_BOUNDARY / current-production-ops.

| phrase | counts per file | total |
|---|---|---|
| 计算角色 | 3,3,3,0,0,0,0,0,0,4 | 13 |
| 计算节点 | 5,5,1,0,0,0,0,0,0,2 | 13 |
| 计算角色主机说明 (banner title) | current-production-ops 1 (title of the #3360 blockquote; the same blockquote text is repeated in the two other full banners) | |
| w3 / w2 | 3,3,2,0,0,0,0,0,0,2 | 10 |
| w3、w2 / w3 和 w2 | w2-w3 doc 4+1, c-p-ops 1 | 6 |
| 原 node-22 主机 | 2,2,0,...,c-p-ops 1 | 5 |
| 原主机 | w2-w3 doc 6, c-p-ops 2 | 8 |
| 历史代号 | 1,1,1 | 3 |
| 控制节点 / Slurm 控制器 / w3（控制） | 1,1 / 1,1,1 / 1,1,..1 | |
| LAN 主机 | 1,1,1,...,1 | 4 |
| compute role | specs: topology-contract 10, ops-readiness 3, unit-tracking 1, python-env 2, ROLE_BOUNDARY 2 | 18 |
| codename | topology-contract 5 | 5 |
| control host | topology-contract 2, unit-tracking 1, ROLE_BOUNDARY 1 | 4 |
| compute host | topology-contract 4 | 4 |
| retired host | topology-contract 4, python-env 1, ROLE_BOUNDARY 1 | 6 |
| dedicated LAN hosts / LAN host | topology-contract 1, ROLE_BOUNDARY 1 each | 2 |
| rollback archive | shared 1, CLAUDE 1, w2-w3 1, topology 2, ops-readiness 1, ROLE_BOUNDARY 1, c-p-ops 1 | 8 |
| compute_control (runtime role identifier) | ROLE_BOUNDARY 3 (repo-wide 52 files / 154 lines) | |

Inconsistencies:
1. Role named "计算角色" in Chinese prose but also "计算节点" for the same set (`shared.md:17` "计算节点 w3（控制）+ w2", :20-22, :73 "**计算节点（w3 / w2）例外**" pinned by `test_node22_entrypoint_invariant.py:526`). "计算节点" also means Slurm worker nodes in older docs (two-node-production-e2e-plan.md:106 "Slurm 计算节点"), a collision.
2. w3 is "控制" (shared.md:17), "控制节点" (:41), "Slurm 控制器" (:20), "control host" (specs), "compute control host" never appears. w2 is never given a short role name.
3. English specs say "compute role", "codename", "control host", "compute host"; Chinese says 计算角色 / 历史代号; the glossary has none (see below).
4. Spec Purpose text says "node-22" is the codename while 5 contexts still use "node-22" as the retired host ("原 node-22 主机", `ROLE_BOUNDARY.md:21-26`, `production-topology-contract` :75). The same token means two things; docs separate them only by the prefix "原".
5. The old shorthand "22 compute-control 节点" (UI) predates all of these and uses the third vocabulary ("compute-control" is also an API role name `compute_control`/"compute-control mode" in 18 files).

`openspec/glossary.md` does not define the compute role, w3, w2, node-22, or the codename rule. It has only "compute plane" x4 and one `node-22 scheduler registry manifest` use (:36). No entry for the retired host.

## 7. Other host tokens carrying the same stale narration

| token | scope (history excluded) | notes |
|---|---|---|
| `210.77.77.22` | 27 files / 71 lines | 35 lines contain `ssh -p 32099 frd_muziyao@210.77.77.22` in docs/runbooks: 31 are command lines in 7 production-ops pages (presented as current), 1 is in the qhh-22 bring-up (historical), 3 are the banner sentences quoting the command; worst: stuck-detection.md (9 commands: 33,104,167,653,656,661,670,673,754), artifacts-and-storage.md (8: 229,310,339,399,468,501,688,840), known-issues-state-index.md (7: 21,60,194,329,593,712,810), service-bringup.md (3: 196,271,330), gateway-and-services.md (2: 124,829), file-provider-refresh.md (1: 982), recalibration-and-archive.md (1: 29). Handled today only by the banners/pointers; no command was changed. Rest: negation/banners (topology specs 4+3, instructions 3, ROLE_BOUNDARY 1, topology doc 2), qhh-22 bring-up (2, historical), display-readonly-live-mvt.md (3, "do not copy"), code sets `scripts/node27_autopipeline.py:225`, `scripts/node27_download_cycles.py:198`, `services/orchestrator/file_orchestration_migration.py:65` (behaviour), tests. |
| `xnode` | 3 lines / 2 files | docs/runbooks/qhh-22-business-bringup.md:49,70 (historical) and docs/VALIDATION.md:127 ("Host/user: `xnode` / `frd_muziyao`"; stale if VALIDATION is current) |
| `10.0.2.100` / `10.0.1.100` | 13 files / 31 lines | historical DB and host addresses; mostly qhh-22 bring-up and spec `node22-db-retirement-cutover` |
| `:55433` / `55433` | 44 files / 183 lines | none presents it as a current DB; all are denial/historical (ROLE_BOUNDARY, retirement runbook/spec, topology spec, display-readonly-live-mvt, tests) or code guards (`scripts/node27_autopipeline.py:233`, `node27_download_cycles.py:198`, `file_orchestration_migration.py:56`); `qhh-22-business-bringup.md:56,72,156` presents it as pre-#837 historical |
| `/volume/nwm` (old host's Basins path) | 12 files / 28 lines | service-bringup.md:326 "2026-06-30 现场 22 节点的 Basins 根为 `/volume/nwm/Basins`"; `workers/mapping_builder/cli.py:17,153,1060` (code comments/help, "node-22 dev tree") |
| bare `22`/`27` shorthand (Chinese; heuristic regex) | 53 lines / 16 files: docs/runbooks 40 lines / 6 files, apps/frontend 9 / 6, openspec/specs 3 / 3, tests 1 / 1 | two-node-deployment-overview.md (19: "22 节点是计算控制面", "22 写入"), two-node-production-e2e-plan.md (14), service-bringup.md (4: :326,365,366,410), forcing-copyback-backfill.md:50, canonical-precip-mirror-backfill.md:128, node-27-bringup-checklist.md:756, 3 specs |
| `22 compute-control` UI string | apps/frontend 5 lines + 3 tests + dom.ts + fake page | JobsTable.tsx:92-93, BasinFailures.tsx:32-33; user-visible on `/ops`; see item 3 |
| `计算控制面` | 4 files / 19 lines | old role name; mostly two-node docs |
| frontend `node-22` narration | apps/frontend 5 lines (comments only: JobsTable.tsx:89, monitoring.mocked.spec.ts:119,166, monitoring.mocked.ts:310, types.ts:463 generated) | |

## 8. Natural PR slices (ordered by dependency)

Slice counts cover only lines matching `node-?22` (the 2507 baseline). Item-7 lines are named per slice where they add to it.

Counts = files / hit lines in the slice (non-identifier lines in brackets). Identifier lines stay untouched.

1. **Gate vocabulary first** (scripts/governance + tests/test_entropy_audit_*, tests/entropy_audit_helpers.py): 11 files / 407 lines [179]. Decision needed: whether to teach the gate "compute role" / "计算角色" / "计算节点" as a subject (items 2a); the 29 gate-vocab lines and about 100 test fixture lines must keep `node-22`. Everything after depends on this: a reworded sentence that drops node-22 / w3 / w2 leaves the real-repo hard gate (`test_entropy_audit_report_contract.py:208`) blind. Behaviour-changing by nature. Do not reword finding titles without checking baseline consumers.
2. **Instructions + glossary terms** (`instructions/agents/shared.md`, CLAUDE.md, AGENTS.md, 6 scoped AGENTS.md with hits, openspec/glossary.md): 10 files / 20 lines [15]. Of the scoped files, apps/api, apps/frontend, services/orchestrator and services/production_closure are governed by `scripts/governance/entropy_audit/scoped_context.py` / `schema.py:141+` (required reference strings such as `docs/runbooks/two-node-deployment-overview.md`, glossary terms, verification commands; `tests/test_entropy_audit_instruction_inventory.py:40` asserts the current repo is clean); none of those required strings contains node-22. Pins: `test_node22_entrypoint_invariant.py:493-552` (byte-exact generated roots; retired-host line must keep `node-22`, `3.12.7`, `#1831`, `不要`, `` `uv` ``). Defines the vocabulary used by 3-5; resolve item-6 inconsistencies here. Cannot drop `node-22` from the :24/:30/:83 lines.
3. **Live docs** (runbooks, ROLE_BOUNDARY, project-profile, infra/env/README, docs/spec, docs/modules; also scripts/diagnostic/qhh/README.md, which `test_qhh_scripts_static.py:636-677` reads but which is counted in slice 5's totals): 36 files / 482 hit lines [310], plus item-7 lines that do not contain the string node-22: 31 `ssh ...@210.77.77.22` command lines and about 26 bare-`22` shorthand lines (two-node-deployment-overview 19, service-bringup 4, forcing/canonical/bringup-checklist 3). Pins: role_boundary_static (4 sentences), section_markers (heading + exact sentence), AUTHORITY_HEADING and 3 anchors, python_scan:243-255, entrypoint_invariant command scans, the 5 never-skipped gate paths. The 31 literal ssh command lines (artifacts 8, stuck-detection 9, known-issues-state-index 7, service-bringup 3, gateway 2, file-provider-refresh 1, recalibration 1) are operator-facing commands, not just prose; the banners currently say to run them on w3 instead. Removing banners contradicts `production-ops-readiness/spec.md:277-287` and `production-topology-contract/spec.md:36-48`, so slice 7 must land in the same PR or earlier.
4. **Historical-only docs** (qhh-22-business-bringup, qhh-continuous, forcing-copyback-backfill, node22-db-retirement-runbook, two-node-production-e2e-plan, qhh-mvp-production-like-e2e-checklist, qhh-mvp-smoke-evidence, progress.md, LEGACY_DEAD_CODE_INVENTORY, GOVERNANCE_8_FINAL_EVIDENCE_MAP, ADRs, docs/ForcingReplace, docs/forcing...): 18 files / 108 hit lines [89], plus 14 bare-`22` shorthand lines in two-node-production-e2e-plan and 1 ssh command line in qhh-22. Gate exemptions: a complete archive-status front matter (qhh-22-business-bringup, qhh-continuous) or a prose declaration in the first 80 lines (forcing-copyback-backfill, qhh-backend-smoke, qhh-mvp-smoke-evidence, qhh-mvp-production-like-e2e-checklist, two-node-production-e2e-plan, two-node-deployment-overview) already skip the file. node22-db-retirement-runbook is the one still scanned; docs/adr, progress.md, docs/ForcingReplace and the other items of this slice are outside the scan set. Pins: `test_qhh_bringup_has_historical_marker`, forcing-copyback "historical|归档", `test_readonly_db_validation_routes.py` fences in two-node-production-e2e-plan. Independent of 3; ADRs and receipts-citing text should arguably be left as history.
5. **Code human text** (apps, packages, services, workers, scripts non-.sh, db, schemas description, openapi chain): 101 files / 302 lines [161]. Mechanical comment/docstring edits are free; but not free: the openapi chain (`openapi_patching_pipeline.py:213` -> `openapi/nhms.v1.yaml:1697` -> `types.ts:463`, regenerate with `check:api-types`); pinned messages (`basin_retirement/run.py:72-73`, `file_orchestration_migration.py:1806,1815`); the 15 persisted/forbidden-host lines (`node27_autopipeline.py:227-228`, `file_orchestration_migration.py:62-65,1337`, schema keys `node22`, `node22_touched`) and `db/migrations/000035...sql:45` (`COMMENT ON` text persisted in fresh databases) must not change. Python comments must not add `file.py:NNN` or backtick `:NNN` tokens (line-reference gate). The frontend UI string is a separable sub-slice (5b: 9 shorthand lines across 6 frontend files: JobsTable.tsx:92-93, BasinFailures.tsx:32-33, JobsTable.test.tsx:239,276, StageCard.test.tsx:126, `c4DisplayEvidence/dom.ts:58`, `c4DisplayFakePage.ts:211`; plus `display-readonly-ops-ui/spec.md:100` from slice 7; these lines do not contain the string node-22 and are not in the slice counts): `dom.ts:58` is a live-evidence probe that matches the rendered text, so changing the UI text without the probe breaks the node-27 C4 live receipt (`docs/runbooks/node-27-bringup-checklist.md:408` refers to the same file); it needs a node-27 live receipt, not local tests.
6. **Infra units, env examples, alert lane** (infra/**, scripts/*.sh): 31 files / 148 hit lines [62]. 14 `Description=` lines and the comments of 15 units are covered by `infra/systemd/node22/MANIFEST.json` sha256 (24 entries; `tests/test_node22_unit_tracking.py` hashes tracked files) and by the drift probe against deployed copies on w3 (`scripts/node22_unit_drift_check.py`): any byte change needs a manifest update and a redeploy to w3 (live effect, scheduler pass boundary). The 7 files under `infra/systemd/node22/` have no narration but are pinned by receipt hashes. Alert lane pins: Subject/body/`SENDER` text (`test_node22_unit_failure_alert.py:71,249,255,292`) and the `node-22` HOST fallback (persisted in mail). Env example comments: `NODE22_*` values are identifiers.
7. **OpenSpec deltas** (openspec/specs + the stale active change): 55 files / 242 lines [205]. Requires an OpenSpec change (new capability text via deltas; archive in the PR's last push). Mandatory: `production-topology-contract`, `production-ops-readiness` (banner requirement), `display-readonly-ops-ui:100`, the present-tense location requirements (slurm-gateway-node22-deployment:9, node22-dbfree-runtime:8, node22-unit-failure-alert:40, production-scheduler-orchestration:13-59, compute-scheduler-operationalization:45,63). Requirement headers containing node-22 (18) need RENAMED deltas, or be kept. The 6 spec names stay. Decide separately whether to archive `fix-node22-keliya-forecast-output-recompute` (13 narration lines, superseded per its tasks.md:20).
8. **Tests** (remaining): 128 files / 798 lines [222]. 198 plain comment/docstring lines are free to reword; 5 exact-literal asserts and the 10 pin-on-prose strings follow slices 2-3; ~100 gate fixture sentences stay (see slice 1). Do last; no behaviour.

Cannot change without behaviour or persisted-value change (summary): gate vocabulary (29 lines), forbidden-host sets and receipt `source` (7 lines), evidence JSON keys `node22`, `node22_touched` (6), mail Subject/HOST fallback (2), the live-evidence UI probe string, the generated openapi/types chain, heading/anchor pins (3 headings, 8 anchor links), manifest-hashed unit bytes (15 files), `COMMENT ON` in a migration.

## Appendix A. Per-file counts, docs/runbooks and other docs (narration / identifier lines)

| file | narr | ident |
|---|---|---|
| docs/runbooks/production-ops/artifacts-and-storage.md | 40 | 0 |
| docs/runbooks/production-ops/service-bringup.md | 31 | 15 |
| docs/runbooks/production-ops/known-issues-state-index.md | 24 | 14 |
| docs/runbooks/production-ops/recalibration-and-archive.md | 21 | 12 |
| docs/runbooks/production-ops/gateway-and-services.md | 15 | 17 |
| docs/runbooks/production-ops/operating-scope.md | 15 | 5 |
| docs/runbooks/production-ops/file-provider-refresh.md | 14 | 19 |
| docs/runbooks/qhh-22-business-bringup.md | 14 | 2 |
| docs/adr/0008-cross-host-copyback-lock-primitive.md | 12 | 0 |
| docs/runbooks/forcing-copyback-backfill.md | 12 | 0 |
| docs/runbooks/tier-node27-timeseries-storage.md | 12 | 0 |
| docs/runbooks/node22-control-plane-manual-recovery.md | 11 | 19 |
| docs/runbooks/production-ops/stuck-detection.md | 11 | 24 |
| docs/governance/ROLE_BOUNDARY.md | 10 | 0 |
| docs/runbooks/node22-db-retirement-runbook.md | 9 | 3 |
| docs/runbooks/production-ops/known-issues-pipeline.md | 9 | 14 |
| docs/runbooks/failed-basin-retry.md | 8 | 5 |
| docs/runbooks/two-node-deployment-overview.md | 7 | 1 |
| docs/adr/0002-node27-timeseries-hot-cold-tiering.md | 6 | 2 |
| docs/runbooks/production-ops/overview.md | 6 | 0 |
| docs/runbooks/qhh-continuous.md | 6 | 1 |
| docs/runbooks/scheduler-dbfree-typed-reasons.md | 6 | 6 |
| docs/ForcingReplace/CMFD 建模资产向 IFSGFS Direct-Grid 的安全迁移.md | 5 | 0 |
| docs/runbooks/canonical-precip-mirror-backfill.md | 5 | 2 |
| docs/runbooks/current-production-ops.md | 5 | 2 |
| docs/runbooks/display-readonly-live-mvt.md | 4 | 0 |
| docs/runbooks/node-27-bringup-checklist.md | 4 | 0 |
| docs/runbooks/qhh-controlled-failure-retry-evidence.md | 4 | 1 |
| docs/runbooks/qhh-mvp-production-like-e2e-checklist.md | 4 | 5 |
| docs/runbooks/slurm-gateway-preflight.md | 4 | 0 |
| docs/spec/03_database_design.md | 4 | 0 |
| docs/adr/0003-review-lens-rotation-keep.md | 2 | 0 |
| docs/adr/0007-shud-openmp-binary-lineage-switch.md | 2 | 0 |
| docs/forcing数据处理流程与rSHUD一致性说明.md | 2 | 0 |
| docs/governance/LEGACY_DEAD_CODE_INVENTORY.md | 2 | 0 |
| docs/runbooks/production-ops/frontier-stall-alert.md | 2 | 0 |
| docs/runbooks/qhh-backend-smoke.md | 2 | 0 |
| docs/runbooks/two-node-production-e2e-plan.md | 2 | 1 |
| docs/adr/0005-recalibration-state-carryover.md | 1 | 3 |
| docs/adr/0009-path-canonicalization-dereference-doctrine.md | 1 | 1 |
| docs/governance/GOVERNANCE_8_FINAL_EVIDENCE_MAP.md | 1 | 0 |
| docs/modules/02_raw_data_ingestion_design.md | 1 | 0 |
| docs/modules/02_raw_data_ingestion_spec.md | 1 | 0 |
| docs/modules/05_model_registry_versioning_design.md | 1 | 0 |
| docs/runbooks/ci-test-routing.md | 1 | 0 |
| docs/runbooks/held-forcing-absence-exit.md | 1 | 0 |
| docs/runbooks/production-ops/parse-failure-residency-alert.md | 1 | 0 |
| docs/runbooks/qhh-mvp-smoke-evidence.md | 1 | 1 |
| docs/runbooks/w2-w3-compute-topology.md | 1 | 3 |
| docs/spec/04_api_design.md | 1 | 1 |
| docs/spec/06_frontend_gis_design.md | 1 | 0 |
| TOTAL | 365 | 179 |

## Appendix B. Per spec: narration (n) / identifier (i) lines, and the nearest `### Requirement:` of each narration line (line numbers in the spec)

```
production-topology-contract n=29 i=1 name_node22=False
    REQ: Current production topology is a governed contract [17, 28, 41, 46, 54, 73, 75, 78, 81, 83]
    REQ: Static guardrails block topology drift [117, 119, 120]
    REQ: Node-22 scheduler consumes node-27 NFS raw manifests [169, 171, 177, 184, 186, 190, 194, 207]
    REQ: Historical node-22 PostgreSQL retirement remains separately gated [209, 211, 218, 222, 231, 236, 239, 242]
node22-unit-failure-alert n=6 i=8 name_node22=True
    REQ: (purpose/preamble) [4]
    REQ: A generic node-22 unit-failure alert template SHALL exist with absolute paths [8, 9]
    REQ: Tracked node-22 oneshot units SHALL be wired to the alert [27, 37]
    REQ: node-22 SHALL reach the SMTPS shim only through the exact-interpreter wrapper [40]
node22-db-retirement-cutover n=11 i=2 name_node22=True
    REQ: Node-22 historical PostgreSQL is stopped only after DB-free proof [8, 10, 16, 27, 37, 46, 53]
    REQ: Guardrails prevent reintroducing active node-22 DB assumptions [58, 61, 64, 70]
production-ops-readiness n=11 i=0 name_node22=False
    REQ: The node-22 refresh timer health installer restores exactly once on an enable fa [187]
    REQ: Current production ops runbook reflects verified 27-centric topology [237, 260, 262, 263, 266, 268, 270, 277, 303, 307]
production-scheduler-orchestration n=9 i=2 name_node22=False
    REQ: Backend scheduler entrypoint [13, 30, 32, 38, 40, 59]
    REQ: Out-of-pass deletion surfaces respect the pipeline frontier or fail closed [146]
    REQ: A narrowed-scope pass SHALL NOT be read as evidence that nothing is pending [1090, 1096]
slurm-gateway-node22-deployment n=9 i=1 name_node22=True
    REQ: (purpose/preamble) [5]
    REQ: A standalone Slurm gateway service is deployed on node-22 [9, 10]
    REQ: Node-22 Slurm mutation defense in depth [52, 60, 63, 68, 71, 75]
node22-unit-tracking n=4 i=5 name_node22=True
    REQ: (purpose/preamble) [4]
    REQ: Live node-22 units SHALL be tracked byte-exactly with a manifest and without sec [8, 9]
    REQ: A read-only, manifest-driven probe SHALL report deployed-versus-tracked drift [56]
compute-scheduler-operationalization n=7 i=1 name_node22=False
    REQ: (purpose/preamble) [4, 6]
    REQ: Node-22 end-to-end automation evidence [45, 46, 61]
    REQ: Node-22 exact-cycle wrapper serializes with the scheduler timer [63]
    REQ: The compute units' environment-file authority is documented [113]
scheduler-registry-refresh n=8 i=0 name_node22=False
    REQ: (purpose/preamble) [5]
    REQ: The refresh timer's liveness is observable from independent signals before the f [508]
    REQ: The tracked DB-free scheduler env template carries every key the documentation d [681, 688]
    REQ: The refresh installer backs out every failure once, reads the result back, and n [736]
    REQ: Refresh execution is bounded database-free and auditable [977]
    REQ: Timer deployment is reversible and has a safe steady state [1040]
    REQ: Every scheduler file-provider writer participates in one fail-safe transaction [1231]
job-retry-mechanism n=7 i=0 name_node22=False
    REQ: (purpose/preamble) [5]
    REQ: Surviving retry-lane consumers of a read-blocked journal row SHALL refuse, never [2572, 2601]
    REQ: DB-free scheduler retry manifests preserve runtime mode [2696, 2725, 2795, 2805]
node22-dbfree-runtime n=6 i=1 name_node22=True
    REQ: Node-22 scheduler has an explicit DB-free runtime mode [8, 10, 33, 69, 77]
    REQ: DB-free runtime evidence proves absence of PostgreSQL use [91]
node27-download-orchestration n=7 i=0 name_node22=False
    REQ: Node-27 download runner owns source acquisition handoff [23, 42, 47, 54]
    REQ: Node-27 production pass drives allowed cycles [59, 72, 76]
scheduler-stall-alerting n=5 i=2 name_node22=False
    REQ: (purpose/preamble) [5, 11, 13]
    REQ: A stalled DB-free scheduler SHALL become a non-zero unit without anyone reading  [21]
    REQ: In-flight-held passes do not clear the stall streak [388]
variant-activation-cutover n=6 i=1 name_node22=False
    REQ: (purpose/preamble) [6]
    REQ: A model lifecycle transition SHALL NOT publish the scheduler file plane [57, 58, 60, 64, 68]
canonical-precip-copyback n=6 i=0 name_node22=False
    REQ: The `convert` stage's terminal entry mirrors canonical precipitation products [15, 39, 41]
    REQ: One-shot backfill mirrors retained cycles without touching the environment [135, 137, 188]
basin-retirement n=4 i=0 name_node22=False
    REQ: (purpose/preamble) [4]
    REQ: Basin retirement on node-27 runs in a fixed, receipt-gated order [16, 22, 27]
entropy-automation n=0 i=4 name_node22=False
file-state-snapshot-index n=3 i=1 name_node22=False
    REQ: (purpose/preamble) [4]
    REQ: State snapshot index capacity SHALL be watched daily with a failing unit as the  [872]
    REQ: State index prune-retention SHALL be enforced automatically at the scheduler pas [976]
forcing-domain-handoff n=4 i=0 name_node22=False
    REQ: Object-store forcing-domain handoff is canonical [12, 98]
    REQ: Archived/stopped compatibility-only node-22 rollback mirror is explicit-DSN, all [101, 103]
node22-env-hygiene n=2 i=2 name_node22=True
    REQ: (purpose/preamble) [4]
    REQ: The live scheduler env drift SHALL be classified against the loader's effective  [16]
pipeline-job-persistence n=4 i=0 name_node22=False
    REQ: pipeline_job Record Creation [10, 43, 45]
    REQ: Expired file-journal cycles SHALL be archived only as complete, inactive, recove [1400]
direct-grid-readiness-evidence n=3 i=0 name_node22=False
    REQ: Minimal-basin staging validation with the production SHUD binary [59, 68, 69]
file-model-readiness n=2 i=1 name_node22=False
    REQ: Scheduler model discovery can use a file-backed registry [10]
    REQ: Scheduler canonical readiness can use a file-backed index [87]
ops-monitoring-api n=2 i=1 name_node22=False
    REQ: (purpose/preamble) [5]
    REQ: POST /api/v1/runs/{run_id}/retry is a fail-closed manual-action endpoint [225]
ops-mvp-pipeline-control n=2 i=1 name_node22=False
    REQ: Ops MVP entry [11]
    REQ: Formal pipeline-backed stage and job status [45]
real-shud-slurm-execution n=3 i=0 name_node22=False
    REQ: Real SHUD executable preflight [23]
    REQ: Node-22 Slurm submission path [41, 42]
scheduler-evidence-retention n=3 i=0 name_node22=False
    REQ: (purpose/preamble) [4]
    REQ: A systemd user timer drives retention [10]
    REQ: Retention receipt is emitted every run [90]
atomic-cutover-transaction n=2 i=0 name_node22=False
    REQ: (purpose/preamble) [4]
    REQ: Model activation SHALL NOT clone state [15]
doc-status-alignment n=2 i=0 name_node22=False
    REQ: active_flag authority is declared [62, 71]
file-orchestration-journal n=2 i=0 name_node22=False
    REQ: Scheduler can use a file-backed orchestration journal [54, 121]
fingerprint-gated-state-clone n=2 i=0 name_node22=False
    REQ: (purpose/preamble) [4]
    REQ: File-index clone invocations preserve durable abort evidence and primary errors [294]
forcing-copyback-backfill n=2 i=0 name_node22=False
    REQ: Node-22 operator documentation describes execution and recovery [66, 68]
model-succession-run n=2 i=0 name_node22=False
    REQ: (purpose/preamble) [5]
    REQ: Compute-side succession steps run in a fixed, receipt-gated order [14]
orchestrator-structural-burndown n=1 i=1 name_node22=False
    REQ: Refresh-timer and cutover-flip corpora SHALL partition without drift [698]
python-environment-truth n=2 i=0 name_node22=False
    REQ: (purpose/preamble) [5]
    REQ: Active node-22 entrypoints preserve the deferred environment [26]
runtime-evidence-and-operations n=2 i=0 name_node22=False
    REQ: Scheduler repair recovery proof binds compute and published progression [545, 547]
scheduler-pass-timing-instrumentation n=2 i=0 name_node22=False
    REQ: (purpose/preamble) [4]
    REQ: Candidate-layer timing is emitted only at `NHMS_SCHEDULER_TIMING_LEVEL=candidate [104]
adapter-cycle-hours n=1 i=0 name_node22=False
    REQ: Production Runbook Separates Artifact Sources [78]
basin-catalog-manifest-audit n=1 i=0 name_node22=False
    REQ: Default basin catalog differences from the scheduler manifest are machine-explai [9]
compute-system-units-tracking n=0 i=1 name_node22=False
filesystem-permission-determinism n=1 i=0 name_node22=False
    REQ: (purpose/preamble) [10]
mapping-builder-cli n=1 i=0 name_node22=False
    REQ: Input authority is the object-store release-frozen package [34]
monitoring-migration n=1 i=0 name_node22=False
    REQ: The jobs table SHALL render no run-action control [83]
node27-raw-retention n=1 i=0 name_node22=False
    REQ: Canonical mirror cycle trees are deletable by the retention account [104]
node27-window-admission n=1 i=0 name_node22=False
    REQ: Child qualification does not claim parent rollout completion [102]
pipeline-monitoring-frontend n=1 i=0 name_node22=False
    REQ: Center Job List Table [153]
precipitation-raster-overlay n=1 i=0 name_node22=False
    REQ: Precipitation endpoints and catalog entry [96]
published-pipeline-job-provenance n=1 i=0 name_node22=False
    REQ: Source-owned published job provenance [10]
real-slurm-gateway-contract n=1 i=0 name_node22=False
    REQ: Default sacct lookback windows are rendered in the host's local wall clock [226]
retry-execution-contract n=1 i=0 name_node22=False
    REQ: The manual-retry preview points a refused hydro run at its cohort master [27]
z-policy-verdict n=1 i=0 name_node22=False
    REQ: z_policy verdict is produced by a narrow three-question solver audit [14]
```
