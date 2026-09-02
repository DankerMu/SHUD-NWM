# Tasks

Fixture level: expanded · repair intensity: medium · issues: #1693 #1694 #1695 #1678
(one PR, one implementer, serial, order #1678 → #1693 → #1695 → #1694).
Line cites are against `origin/master` `e0a655ac`; symbol names are authoritative.
Upstream `Suggested fixture level` / `Minimal mergeable slice`: absent on all four
(hand-written issues). Expanded trigger: #1678 touches the public OpenAPI contract
(`public API`, `schema`); the other three are docs plus comment-only code edits.

Change surface: `apps/api/openapi_patching.py`, `openapi/nhms.v1.yaml`,
`apps/frontend/src/api/types.ts` (only if `openapi-typescript` output changes),
`tests/test_api_contract.py`, `tests/test_basins_registry_import.py`,
`workers/model_registry/basins_geometry.py` (comment), `workers/model_registry/basins_reingest.py`
(comment), `openspec/glossary.md`, `docs/runbooks/current-production-ops.md`,
`docs/spec/03_database_design.md`, `infra/env/README.md`, `infra/env/compute.example`.
Orchestrator-only ops surface: node-22 `infra/env/compute.env` (gitignored).

Must preserve: `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema`
(static == runtime, full dict); every existing case in
`test_display_control_plane_responses_have_no_static_runtime_drift`; the `503`
`ControlPlaneQueueUnavailable` declaration on `GET /api/v1/queue/depth`; all import /
parser / backfill behavior in `workers/model_registry/**` and `workers/output_parser/**`
(`git diff` on those trees is comment-only); every existing assertion in the extended
import test; every existing key in `infra/env/compute.example`; `redocly lint` at
0 errors; no `--skip-rule` / `--ignore` additions.

Seams under test: `app.openapi()` vs `yaml.safe_load(openapi/nhms.v1.yaml)` (drift);
`_response_error_codes` in `tests/test_api_contract.py` (per-status code enums);
`integration_database_url` + `basins_registry_import` CLI in
`tests/test_basins_registry_import.py` (row-class counts on the imported rnv);
`redocly lint` CLI at `@redocly/cli@1.25.13` (warning inventory).

Risk packs (core):
- Public API / CLI / script entry: **selected** — new 502/504 declarations on `GET /api/v1/queue/depth`; evidence 0.1, 0.2, 0.5.
- Config / project setup: **selected** — `compute.example` values and README authority table; evidence 0.7, 0.8.
- File IO / path safety / overwrite: not selected — no IO code changes; the live env edit is an orchestrator ops step with backup (0.8).
- Schema / columns / units / field names: not selected — documenting existing columns; no schema change.
- Auth / permissions / secrets: not selected — no auth surface; live env edit must not print `DATABASE_URL` (0.8 receipt is `grep -nE 'BASIN|MODEL_IDS'` only).
- Concurrency / shared state / ordering: not selected.
- Resource limits / large input / discovery: not selected.
- Legacy compatibility / examples: **selected** — `compute.example` is a tracked example; frontend generated types may shift; evidence 0.3, 0.7.
- Error handling / rollback / partial outputs: not selected — error *declarations* change, error *behavior* does not (D5 raise-site audit is the proof).
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: **selected** — four spec deltas plus glossary charter; evidence 0.4, 0.6, 0.9.
Domain packs (NHMS profile): all not selected — no geospatial, forcing, SHUD-numerical,
PostGIS/Timescale runtime, Slurm scheduling, provider, manifest/QC, or display behavior
changes; #1693's pin reads rows the existing import test already writes.

Non-goals: any DB row/schema/migration change (incl. `COMMENT ON COLUMN`); flipping any
`active_flag`; a 4XX declaration on any of the three lint-flagged GETs; `info.license`
(owner decision); a tracked template for node-22 `compute.host.env` (reported);
deleting keys from node-22 `compute.env`; touching `compute.scheduler-dbfree.env`.

Recorded reasons (no issue filed, per user instruction to resolve in-batch):
- `info.license` absent: no `LICENSE`, no `pyproject` license, frontend `ISC` is an npm
  scaffold default; writing a value would fabricate a legal decision. #1678 stays open on
  this one criterion until the owner answers.
- `COMMENT ON COLUMN core.basin_version.active_flag` not added: needs a migration plus
  node-27 apply for a sentence the spec now carries; can be added later.
- `compute.host.env` has no tracked template: out of #1694 scope; recorded in README
  table as untracked and in the work summary.

## 0. Evidence Floor

Expected `redocly` output after this change is **0 errors, 4 warnings** — the same four
as before: `operation-4xx-response` on `GET /api/v1/queue/depth`, `GET /api/v1/slurm/health`,
`GET /health` and `info-license` (owner-pending).
This is the pinned, justified state, not a regression. Per-operation reason no 4XX is
reachable (raise-site audit in design.md Context, re-verified by round-1 reviewers):

- `GET /api/v1/queue/depth`: no parameters (no 422), no auth dependency (mutation guard
  only), the display guard raises 503, and `gateway.list_jobs` raises only 502/504
  subclasses (`SlurmCommandError`, `SlurmParseError`, `SlurmTimeoutError`); a gateway
  construction failure is a plain `ValueError` → unhandled 500.
- `GET /api/v1/slurm/health`: both backends' `health()` return an unhealthy 200 body
  instead of raising; no parameters, no dependencies; `create_gateway` failures are
  unhandled 500, not 4XX.
- `GET /health`: static dict handler in `apps/api/startup_wiring.py`, no inputs.
- `#/info` license: owner decision pending (see Recorded reasons).

- [x] 0.1 `uv run pytest -q tests/test_openapi_drift.py tests/test_api_contract.py tests/test_openapi_31_contract.py tests/test_monitoring_api.py tests/test_gateway.py tests/test_runtime_mode.py` green locally
- [x] 0.11 (added after round 1, cand-07; 746 passed / 3 skipped locally, CI selection 1791 passed / 38 skipped) consumers of `infra/env/compute.example` green: `uv run pytest -q tests/test_two_node_docker_runtime.py tests/test_role_boundary_static.py tests/test_slurm_gateway_deployment_contract.py tests/test_two_node_docker_runbook_environment_invariant.py tests/test_two_node_docker_source_trust.py`, plus the full `scripts/select_ci_tests.py` selection for this diff
- [x] 0.12 (added after round 1, cand-09; red proof: injected 403 → `assert ['403'] == []`) executable oracle for the no-4XX invariant: `tests/test_api_contract.py::test_operations_without_reachable_4xx_declare_none` green, red when a 4XX is injected on either side
- [x] 0.2 `npx --yes @redocly/cli@1.25.13 lint openapi/nhms.v1.yaml --skip-rule no-unused-components --max-problems 1000` → 0 errors, exactly the 4 warnings named above
- [x] 0.3 `cd apps/frontend && corepack pnpm run check:api-types` green (regenerate `src/api/types.ts` via `pnpm generate:api` and commit if output changed)
- [x] 0.4 `uv run ruff check .` and `openspec validate record-registry-design-facts-and-api-error-contract --strict --no-interactive` green
- [x] 0.5 Red proof for the new contract cases: with the `_patch_pipeline_openapi` injection reverted, the two new `test_api_contract.py` cases fail on the runtime side (batched red run against pre-change source, output in the implementer report)
- [x] 0.6 node-27 (oracle for real-DB pytest; receipt `.workplans/pr-1956/node27-receipts.log`: green `1 passed` at 60b986d3, bec26967 and 04edab1e in a detached worktree, red proof `assert 10 == (1 * 5)` at the first two (`bec26967..04edab1e` is docs + one comment-only hunk in `basins_geometry.py`, so the red proof was not repeated); read-only all-rnv check at bec26967 and again at 04edab1e with the SQL echoed and column headers: `rnv_total=43`, 43 with `count(*) == 2 × segment_count`, 43 with `output_rows == segment_count`, `violator_count=0`): `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<node-27 scratch url> uv run pytest -q -m integration tests/test_basins_registry_import.py -k "<extended test name>"` green at the reviewed SHA; red proof = the new `2 × segment_count` assertion fails when its expected factor is mutated to 1
- [x] 0.7 `grep -n '/volume/data/nwm' infra/env/compute.example` empty; `grep -n '^NHMS_BASINS_ROOT=' infra/env/compute.example infra/env/compute.scheduler-dbfree.env.example` shows the same value
- [x] 0.8 node-22 receipt (orchestrator, `.workplans/pr-1956/node22-compute-env-receipt.log`, 2026-09-02T10:42Z, plus the 12:36Z read-only block added after the Phase 6.2 audit: `docker ps` empty, `nhms-compute-compose.service` not-found, login-host path existence for `/volume/data/nwm{,/Basins}` MISSING / `/volume/nwm/Basins` EXISTS / `/ghdc/data/nwm/Basins` EXISTS (44 vs 33 top-level entries) / model-asset placeholder MISSING; `nhms-scheduler-journal-retention.service` from the tracked `infra/systemd/` is `LoadState=not-found` on node-22, so it is correctly absent from the README table): `systemctl --user show <unit> -p EnvironmentFiles` for the five units matches the README table; `grep -nE 'BASIN|MODEL_IDS' infra/env/compute.env` before/after shows the three values aligned and the header present; `stat -c %a` of the edited file and its backup both `600`
- [x] 0.9 node-27 read-only receipt (2026-09-02T11:13Z at 60b986d3, 12:15Z at bec26967 and 14:53Z at 04edab1e, identical; the Phase 8 final-head re-check is the 14:53Z run unless a later commit touches non-doc files: basin_version 0/44; model_instance baseline 38 t / 6 f, dg 0 t / 153 f — matches docs/spec/03): `select count(*) filter (where active_flag), count(*) from core.basin_version` and the `model_instance` baseline/dg breakdown match the numbers quoted in `docs/spec/03_database_design.md` (with date)
- [x] 0.10 markdown lint clean on the touched `docs/**` and `infra/env/README.md` (`markdownlint-cli2` per `.markdownlint.yaml`)

## 1. #1678 OpenAPI error contract

- [x] 1.1 In `apps/api/openapi_patching.py::_patch_pipeline_openapi` add components `responses.SlurmGatewayUpstreamError` (502; `_typed_error_response`, codes `SLURM_COMMAND_ERROR`, `SLURM_PARSE_ERROR`) and `responses.SlurmGatewayTimeout` (504; code `SLURM_TIMEOUT`); inject both on `GET /api/v1/queue/depth` beside the existing 503 (`_inject_operation_responses`, `:754-765`)
- [x] 1.2 Mirror both components and both operation responses in `openapi/nhms.v1.yaml` (hand-edit, follow the `ControlPlaneQueueUnavailable` pattern; `test_openapi_drift.py` is the oracle)
- [x] 1.3 Extend `tests/test_api_contract.py::test_display_control_plane_responses_have_no_static_runtime_drift` with `("/api/v1/queue/depth","get","502",[...two codes...])` and `("/api/v1/queue/depth","get","504",["SLURM_TIMEOUT"])`; keep existing single-code cases passing
- [x] 1.4 Do NOT declare a 4XX on `GET /api/v1/queue/depth`, `GET /api/v1/slurm/health`, `GET /health`; do NOT add `info.license`; do NOT touch lint flags — the implementer report cites the raise site (file:line) for every status code the diff declares
- [x] 1.5 Run 0.1, 0.2, 0.3, 0.5

## 2. #1693 river_segment row classes

- [x] 2.1 `openspec/glossary.md`: rewrite the intro so the file is the single source for governance and domain ubiquitous language; add `## Domain terms` with `SHUD input reach row`, `SHUD output river row`, `segment_count` (rnv column, reach rows only), `output_segment_count` (not an rnv column; receipt → `resource_profile` → scheduler manifest fields); keep every existing governance term and the Usage Rules
- [x] 2.2 `docs/runbooks/current-production-ops.md`: add a subsection right after the Heihe `shud_output_river` query (`:3676-3684`) that states the invariant, the hygiene query pattern (filter by `COALESCE(properties_json->>'shud_output_river','false')` then compare to `segment_count`; unfiltered `2 ×` is expected), the `output_segment_count` note, and back-links #1122 / #1123 plus the correctly filtered oracles (`basins_registry_import.py:610-620`, `tests/test_real_database_integration.py:448-453`; `workers/output_parser/parser.py::load_river_segments` only as "output-class-first with unfiltered fallback", see design D2); do not cite `scripts/node27_archive_rebuild_drill.py` (deleted)
- [x] 2.3 `workers/model_registry/basins_geometry.py:126-129`: correct the stale comment (`segment_count` = `gis/river.shp` reach records post-PR-2, equal to the `.sp.riv` count by validation at `:796-806`); `basins_reingest.py:348-359`: add one line pointing at the glossary terms; behavior and assertions unchanged
- [x] 2.4 `tests/test_basins_registry_import.py` (real-DB test around `:3473`): add `total_rows == 2 * segment_count` and `output_rows == reach_rows` assertions for the imported rnv, comment `# #1693: two row classes under one rnv by design`
- [x] 2.5 Run 0.6 (node-27, orchestrator) — implementer runs the test locally only if a PostGIS DB is available, otherwise reports it as node-27-owned

## 3. #1695 active_flag authority

- [x] 3.1 `docs/spec/03_database_design.md`: after the `core.basin_version` block (§5.2) and the `core.model_instance` block, add an "authority" note per design D3 (basin_version flag: no compute authority and not a display-membership flag — importer writes `false`, no UPDATE path, read as the `model_registry.py:874` ORDER BY tiebreak, passed through `GET /api/v1/basins/{basin_id}/versions` to the frontend default-version pick; model_instance flag: display membership `services/tiles/mvt.py:367` and lifecycle API; compute authority: file-registry manifest, DB-free scheduler reads neither DB flag; the two planes are not synchronized); quote the 2026-09-02 node-27 counts with date
- [x] 3.2 `openspec/glossary.md` `## Domain terms`: three `active_flag` entries (file-registry manifest / `core.model_instance` / `core.basin_version`) naming reader and authority, plus one disambiguating line that `met.met_station.active_flag` is a separate station-selection flag (design D3); phrase the basin_version fact as "importer writes a hardcoded `false`" (`basins_registry_import.py:542-548`), not "never sets it"
- [x] 3.3 `docs/runbooks/current-production-ops.md:1223` and `:2854`: append a link to the §5.2 authority note (no other text change)
- [x] 3.4 No DB write, no migration

## 4. #1694 node-22 env authority

- [x] 4.1 `infra/env/README.md`: add a "node-22 unit → EnvironmentFile" table (five units, files, tracked template or "untracked, no template" for `compute.host.env`, scheduler drop-in note) and one sentence that `compute.env` is the compose-lane instance no node-22 unit reads; state the scheduler basin root is the `compute.scheduler-dbfree.env` value (live `/volume/nwm/Basins` as of 2026-09-02) and that `/ghdc/data/nwm/Basins` is the separate NFS root node-27 ingest reads
- [x] 4.2 `infra/env/compute.example`: header pointer to the authority table; `NHMS_BASINS_ROOT=/volume/nwm/Basins`; `NHMS_MODEL_ASSET_ROOT=/scratch/frd_muziyao/nhms-production/model-assets`; no other value changes
- [x] 4.3 Orchestrator ops step on node-22 (done 2026-09-02T10:42Z, backup `compute.env.bak-1694-20260902T104210Z`, receipt in `.workplans/pr-1956/node22-compute-env-receipt.log`) (not implementer): backup `compute.env` → `compute.env.bak-1694-<UTC>` (`cp -p`), prepend the 3-line header, set the three values per design D4, capture the 0.8 receipt
- [x] 4.4 Run 0.7, 0.10

## 5. Close-out

- [x] 5.1 Implementer report lists changed files, verification output, red proofs (0.5), the raise-site table for 1.4, and every deviation (or "no deviations")
- [x] 5.2 PR body `偏离记录` seeded; #1678 premise correction (issue's 403/422 guesses vs. audited 502/504/none) recorded there and in the #1678 closure comment
