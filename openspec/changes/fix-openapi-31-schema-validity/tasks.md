## 0. Risk and evidence contract

Fixture level: **expanded**. Repair intensity: **high**. Project profile: NHMS. Upstream suggested level: absent.

Selected packs: Public API; Config/project setup; File IO/generated artifact; Schema/field names; Auth/permissions/secrets; Legacy compatibility; Error handling/partial outputs; Release/dependency compatibility; Documentation. Not selected: Concurrency and Resource limits (no such behavior); all eight NHMS domain packs (no domain behavior).

Must preserve: route/request/response behavior; auth/RBAC decisions and 401/403/503/allow outcomes; all 109 nullable value domains; exact runtime/static parity; byte-identical `apps/frontend/src/api/types.ts`; existing API selector consumers and PR/job conditions outside the new OpenAPI route.

## 1. Contract tests first

- [x] 1.1 Add runtime/public-schema tests that pin OpenAPI 3.1, recursively reject every `nullable`, prove the 109-node baseline (108 ordinary + one composed), preserve ordinary sibling keywords, normalize nested/false/existing-union/type-array boundaries, and fail loudly on unsupported shapes.
- [x] 1.2 Add a composed-node regression proving `Layer.metadata` remains `LayerMetadata | null`; a constructed naive type-array/allOf mutant generates the wrong intersection and reds.
- [x] 1.3 Add server/security tests pinning same-origin `/`, root `security: []`, exact conditional scheme descriptions, the exact 11 protected operations and complete live-proof AND group, public-operation inheritance, no secret values, and existing 401/403/409/503/allow observables.
- [x] 1.4 Add one shared published-contract offender helper and anti-vacuous mutation proofs for reintroduced nullable/finalizer loss, missing/wrong server, missing/false-global root policy, missing protected override/scheme/live-header leg, split AND group, public override spread, and credential-looking values.
- [x] 1.5 Add selector/workflow tests proving `openapi/**` opens the backend gate and selects exactly drift + API contract + 3.1 dialect/security suites without smoke fallback; patch-owner selection adds both invariant suites while retaining existing API tests; removing the filter or either contract-suite leg reds.
- [x] 1.6 Capture the pre-fix red: pinned Redocly with `--max-problems 1000` reports 164 errors (109 spec + 54 security-defined + 1 no-empty-servers) and four warnings; existing drift/API tests pass the invalid baseline, while the new contract suite initially reports 12 failed / 7 preserve-green.

## 2. Runtime owner and artifacts

- [x] 2.1 Add one deterministic, idempotent finalizer after all OpenAPI patches: 108 ordinary typed nullable nodes become type unions; the one type+allOf node becomes composition-or-null; nested/type-array/null-exactly-once and fail-loud cases preserve the declared value domain.
- [x] 2.2 Invoke the finalizer through both the direct patch pipeline and `apps.api.main` monkeypatch-compatible facade sequence; preserve facade and runtime/static drift tests.
- [x] 2.3 Add truthful same-origin server, public-by-default root security, environment-conditional credential scheme descriptions, and explicit independent-copy overrides for exactly the 11 enforced operations; runtime enforcement remains unchanged.
- [x] 2.4 Regenerate `openapi/nhms.v1.yaml` from a cache-cleared `app.openapi()` and prove semantic equality; no YAML-only fix.
- [x] 2.5 Prove `apps/frontend/src/api/types.ts` is byte-identical after regeneration; both Python generator seams use hosted-runner-resolvable `npx --yes openapi-typescript@7.13.0` and no generator churn is committed.
- [x] 2.6 Add `openapi/**` to the backend paths filter and exact selector routes for the schema artifact and patch owner without weakening existing filters/rules.

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_openapi_31_contract.py tests/test_openapi_drift.py tests/test_api_contract.py tests/test_api.py tests/test_monitoring_api.py tests/test_select_ci_tests.py` passes: 383 assertions.
- [x] 3.2 `npx --yes @redocly/cli@1.25.13 lint openapi/nhms.v1.yaml --skip-rule no-unused-components` exits zero with zero errors; four pre-existing non-failing warnings remain routed out of scope.
- [x] 3.3 `cd apps/frontend && corepack pnpm run check:api-types && corepack pnpm test && corepack pnpm build` passes (326 frontend assertions and successful production build), and `src/api/types.ts` has no diff.
- [x] 3.4 `uv run ruff check .`, `git diff --check`, and `openspec validate fix-openapi-31-schema-validity --strict --no-interactive` pass.
- [x] 3.5 OpenAPI-only selector probe reports exactly three assertion suites; patch-owner probe reports five; required normalization/security/routing/generator mutants independently red and restore.
- [ ] 3.6 Final-head PR CI shows OpenAPI Validate, targeted Unit Tests, Frontend Build, and Governance Audit at terminal success on the same SHA.

## 4. Non-goals and routing

- [x] 4.1 Do not downgrade to OpenAPI 3.0.3, disable Redocly rules, alter auth policy/routes/statuses, change dependencies, add schedule, or touch DB/display/Slurm/SHUD behavior.
- [x] 4.2 The four non-failing Redocly warnings (license and three missing 4XX responses) are deduplicated and routed to #1678; they do not justify weakening or expanding this error repair.
