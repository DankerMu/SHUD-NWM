# QHH MVP smoke evidence freeze

最后更新：2026-05-26

## 结论

Issue #214 只冻结 MVP smoke/evidence 边界，不声明 final production readiness。每个 readiness 相关表述必须指向下表的稳定证据 ID，并保留 mode、command、artifact path、claim boundary 和当前状态。`deterministic`、`mocked`、`partial`、`skipped` 或 `blocked` 项不能升级为 live QHH/Slurm/GFS/IFS 或最终生产就绪证明。

## Evidence matrix

| ID | Surface | Mode | Command | Artifact path | Current status | Claim boundary |
| --- | --- | --- | --- | --- | --- | --- |
| Q214-GFS-01 | QHH GFS cycle download -> canonical -> forcing -> SHUD -> parse -> publish | live diagnostic | `export DATABASE_URL=$(./scripts/local_pg.sh url) && ./scripts/run_qhh_cycle.sh gfs 2026052100` | `.nhms-runs/qhh-smoke/`, `.nhms-runs/qhh-continuous/state/qhh-continuous-summary.json`, `docs/runbooks/qhh-backend-smoke.md`, `docs/runbooks/qhh-continuous.md` | Existing diagnostic evidence for `2026052100` and `2026052106`; not rerun in #214 | Proves qhh diagnostic/reproduction script path can complete for recorded cycles. It is not formal scheduler readiness and not final production readiness. |
| Q214-IFS-01 | QHH IFS cycle download -> canonical -> forcing -> SHUD -> parse -> publish | live diagnostic | `export DATABASE_URL=$(./scripts/local_pg.sh url) && ./scripts/run_qhh_cycle.sh IFS 2026052106` | `.nhms-runs/qhh-continuous/state/cycles/ifs/2026052106.json`, `docs/runbooks/qhh-continuous.md` | Existing diagnostic evidence for `2026052100` and `2026052106`; not rerun in #214 | Proves recorded IFS diagnostic cycles can complete. It is not live production scheduler readiness or future IFS availability proof. |
| Q214-IFS-02 | IFS 06/18 shorter-horizon behavior | deterministic browser/API fixture plus existing diagnostic boundary | `cd apps/frontend && corepack pnpm test:e2e -- hydro-met.spec.ts --project=chromium --workers=1` | `apps/frontend/e2e/hydro-met.spec.ts`, `.codex/evidence/issue-214/hydro-met-e2e.log` | Passed locally in #214: 1 Playwright test passed | Proves `/hydro-met` labels 144h actual horizon against 168h expected horizon for IFS fixture. It does not prove a live 18Z IFS download or SHUD run. |
| Q214-HM-01 | `/hydro-met` browser smoke | deterministic mocked `/api/v1/**` | `cd apps/frontend && corepack pnpm test:e2e -- hydro-met.spec.ts --project=chromium --workers=1` | `apps/frontend/e2e/hydro-met.spec.ts`, `.codex/evidence/issue-214/hydro-met-e2e.log` | Passed locally in #214: 1 Playwright test passed | Proves latest-product bootstrap, station inventory, station-series six forcing variables, `q_down` forecast-series, no-fake-data copy, and GFS/IFS UI wiring under mocked responses only. |
| Q214-OPS-01 | `/ops` controlled failure and retry browser smoke | deterministic mocked browser plus backend fixture tests | `cd apps/frontend && corepack pnpm test:e2e -- monitoring.spec.ts --project=chromium --workers=1` | `apps/frontend/e2e/monitoring.spec.ts`, `docs/runbooks/qhh-controlled-failure-retry-evidence.md`, `.codex/evidence/issue-214/ops-e2e.log` | Passed locally in #214: 11 Playwright tests passed | Proves UI/API wiring for failed row, logs, authorized retry request and retry job terminal outcome in deterministic fixtures. It does not prove live Slurm/QHH retry. |
| Q214-BE-01 | Backend/API deterministic tests | deterministic local test DB/fixtures | `uv run pytest -q tests/test_api.py tests/test_gateway.py tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py` | `.codex/evidence/issue-214/backend-api-tests.log` | Passed locally in #214: 116 tests passed | Proves backend API contracts under deterministic fixtures only. Missing live target DB/Slurm/source receipts remain blockers for production readiness. |
| Q214-OSPEC-01 | OpenSpec validation | static validation | `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive` | `.codex/evidence/issue-214/openspec-validate.log` | Passed locally in #214: change is valid | Proves the M21 OpenSpec change is internally valid. It does not prove runtime behavior. |
| Q214-OAPI-01 | OpenAPI lint and frontend API type drift | static contract check | `npx --yes @redocly/cli@1.25.13 lint openapi/nhms.v1.yaml --skip-rule no-unused-components && cd apps/frontend && corepack pnpm run check:api-types` | `.codex/evidence/issue-214/openapi-api-types.log` | Passed locally in #214: OpenAPI valid and generated API types match | Proves schema/type consistency. It does not prove endpoint availability or live data. |
| Q214-FE-01 | Frontend unit tests and build | deterministic local frontend | `cd apps/frontend && corepack pnpm test && corepack pnpm build` | `.codex/evidence/issue-214/frontend-test-build.log` | Passed locally in #214: 536 tests passed and build succeeded | Proves frontend unit/build health. It does not prove live backend, live map tiles, or production deployment. |
| Q214-MD-01 | Markdown/static docs check | static lint | `npx --yes markdownlint-cli2 docs/runbooks/qhh-mvp-smoke-evidence.md docs/runbooks/qhh-backend-smoke.md docs/runbooks/qhh-continuous.md docs/runbooks/qhh-controlled-failure-retry-evidence.md docs/plans/2026-05-25-mvp-launch-plan.md` | `.codex/evidence/issue-214/markdownlint.log` | Passed locally in #214: 0 errors | Proves focused docs formatting only. It does not prove evidence truth. |
| Q214-DIFF-01 | Whitespace/static diff hygiene | static lint | `git diff --check` | `.codex/evidence/issue-214/git-diff-check.log` | Passed locally in #214: exit code 0 | Proves the current diff has no whitespace errors. It does not prove runtime behavior. |
| Q214-LIVE-01 | Opt-in live MVP smoke | opt-in live, skipped/blocked until target dependencies exist | `NHMS_RUN_QHH_MVP_LIVE_SMOKE=1 uv run nhms-production validate-readiness --evidence-root artifacts/production-closure --run-id qhh-mvp-live-<date>` plus a target-env `uv run nhms-pipeline plan-production --plan ...` receipt | `.codex/evidence/issue-214/opt-in-live-smoke/manifest.md`, `artifacts/production-closure/<run_id>/readiness/`, `<workspace_root>/scheduler/evidence/<pass_id>.json` | Skipped/blocked in this PR: no accepted target live DB, Slurm, object store, source, IdP, alert sink, rollback, or browser receipts were executed | Only a future accepted live receipt can support live production readiness. This row is a blocker/skipped item, not a readiness claim. |

## Required live blockers

The following external/live steps were not executed by #214 and remain blockers or skipped evidence, not readiness claims:

- live target PostgreSQL/PostGIS/TimescaleDB receipt.
- live object store and shared Slurm log root receipt.
- live `nhms-pipeline plan-production --plan` scheduler submission receipt.
- live Slurm `sbatch`/`squeue`/`sacct`/`scancel` receipt for MVP operations.
- live new-cycle GFS/IFS source download receipt.
- live QHH SHUD runtime receipt tied to formal pipeline persistence.
- live `/hydro-met` browser run against the target backend.
- live `/ops` retry/cancel run with target IdP/operator identity.
- live alert sink, rollback, nationwide MVT/PBF and final production readiness receipts.

## Claim rules

- Existing QHH GFS/IFS `2026052100` and `2026052106` evidence is live diagnostic/reproduction evidence only.
- The formal production scheduler path remains `uv run nhms-pipeline plan-production`; qhh scripts remain diagnostic, regression and evidence collection tools.
- `/hydro-met` deterministic browser evidence proves UI wiring and no-fake-data behavior under mocked API responses only.
- `/ops` deterministic evidence proves controlled failure/retry UI and API contract semantics only.
- `no_frequency_curve` is an accepted quality state for QHH MVP display; it never proves real return periods or warning levels.
- Internal MVP readiness may rely on deterministic evidence plus explicit live blockers. Final production readiness requires accepted live receipts for all required external dependencies.
