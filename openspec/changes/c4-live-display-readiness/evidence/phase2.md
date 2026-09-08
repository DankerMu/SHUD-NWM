# #2123 Phase 2 local verification

Implementation tested: `97c7ec4e4bd05f660aaf35b63385b5e44b465b2e` (base `e6b5e4ffd06c6e6b18824ec51082ff4965a1c42a`). All commands serial, no concurrent suites. Evidence-only follow-up commits do not change this tested implementation.

| Command / surface | Result |
| --- | --- |
| frontend `corepack pnpm install --frozen-lockfile` | PASS; first attempt on a71232f7, dependency files unchanged by fix |
| frontend `corepack pnpm typecheck` | PASS; tsc app project |
| frontend `corepack pnpm run check:types` | PASS; tsc Playwright tooling project |
| frontend `corepack pnpm build` | PASS; 5.30s |
| frontend `corepack pnpm test` | PASS; 68 files, 853 tests, empty PLAYWRIGHT_BROWSERS_PATH |
| frontend `corepack pnpm check:bundle` | PASS; 202.7 KB gzip under existing 500 KB limit; existing heavy-vendor exclusions unchanged |
| `uv run --no-sync check-jsonschema --check-metaschema schemas/frontend_c4_live_evidence.schema.json` | PASS |
| `uv run --no-sync check-jsonschema --schemafile schemas/frontend_c4_live_evidence.schema.json` for each C4 PASS/FAIL/BLOCKED example | 3 PASS |
| `uv run --no-sync ruff check tests/test_select_ci_tests.py` | PASS |
| `openspec validate c4-live-display-readiness --strict --no-interactive` | PASS |
| `git diff --check e6b5e4ffd06c6e6b18824ec51082ff4965a1c42a HEAD` | PASS |

Local raw logs: ignored `artifacts/c4-phase2-97c7ec4e/` (results.json, static-results.json, per-command logs); initial failed app-type attempt retained in `artifacts/c4-phase2-a71232f7/`. No claim that ignored logs are available from GitHub.

Initial app typecheck on a71232f7 failed on four C4 fixture typing errors. Fix 97c7ec4e only added required null failure fields, observation type imports and local narrowing of optional fake sleep. No assertion/production/schema/CI relaxation. Installation was not repeated; failed app-type row was rerun once by orchestrator before the remaining rows. Implementer's focused typecheck confirmation is not a substitute for this independent run.

Red-proof: four isolated shipping test suites have green baselines and reject removed role-override/no-clobber/distinct-job/dual-field guards; see implementation-wiring-20260907.md. This is selected red coverage, not a claim that every mutation was tested.

Acceptance mapping: full frontend suite executes config/CLI/static discovery/type includes, lane/owner deadline and failure cases, publisher/binder/JSON/schema negatives, and RBAC/store compatibility. Cross-field SHA/digest belongs to the external #1895 G0/C3 chain (contract-disposition.md), not this C4 CLI.

Outstanding: Python selector C4 CI-scope test has not run locally (backend oracle routing). CI must execute it; collect-only does not satisfy it. Comprehensive review/independent verdicts/Gap Sweep/branch-tip integrity remain separate gates. No node-27/node-22 access, DB, deployment or live receipt. #1895/#1891 remain open.
