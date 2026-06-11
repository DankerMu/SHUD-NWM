## 1. Import Inventory

- [x] 1.1 For #417, run a focused search for `apps.api` imports outside `apps/api` and record exact file/line/import evidence.
- [x] 1.2 For #417, run entropy audit JSON and extract current `apps-api-layer-inversion` findings.
- [x] 1.3 For #417, confirm whether `services/tiles/mvt.py` and `services/production_closure/readonly_db_validation.py` are the only current findings.
- [x] 1.4 For #417, record owner area and confirm the implementation split for #418 tile helper boundary work and #419 readonly validation boundary work.
- [x] 1.5 For #417, record any additional findings as separate follow-up issues instead of expanding the implementation scope silently.

## 2. Tile Helper Boundary Fix

- [x] 2.1 For #418, remove `apps.api.*` imports from `services/tiles/mvt.py`.
- [x] 2.2 For #418, introduce a lower-layer tile/domain exception or helper that carries status code, stable error code, message, and details without depending on `apps.api`.
- [x] 2.3 For #418, adapt `apps/api/routes/flood_alerts.py` so tile/domain exceptions map to the existing `ApiError` response shape at the API boundary.
- [x] 2.4 For #418, run focused tile/API tests and confirm public route behavior is unchanged.
- [x] 2.5 For #418, confirm the entropy audit no longer reports `services/tiles/mvt.py` while leaving #419 readonly validation findings for the next issue.

## 3. Production Closure Boundary Fix

- [x] 3.1 For #419, remove `apps.api.*` imports from `services/production_closure/readonly_db_validation.py`.
- [x] 3.2 For #419, replace the existing readonly validation API-probe exception with an API-owned adapter or injected requester; do not move FastAPI application construction or API route modules into `packages/common`.
- [x] 3.3 For #419, preserve route smoke and retry/cancel manual-action evidence behavior, including readonly display env, bounded database URL, operator headers, and no write-dependency construction.
- [x] 3.4 For #419, run focused readonly validation and production-closure tests.
- [x] 3.5 For #419, update `docs/governance/ROLE_BOUNDARY.md` so it no longer documents a stale service-layer exception after the code boundary changes.

## 4. Enforcement Prep

- [ ] 4.1 For #420 after #418/#419, extend static or entropy tests to prove `apps-api-layer-inversion` is zero for current code.
- [ ] 4.2 Confirm `apps-api-layer-inversion` remains a standalone role-boundary finding family and is not merged into API retirement or display cleanup.
- [ ] 4.3 Update entropy budget docs to state that layer inversion is a future hard-gate candidate only after baseline cleanup.
- [ ] 4.4 Keep `.github/workflows/governance.yml` report-only; do not enable hard-gate mode.

## 5. Verification

- [ ] 5.1 Run `uv run --no-sync pytest -q tests/test_role_boundary_static.py tests/test_entropy_audit_script.py`.
- [ ] 5.2 Run focused backend tests for tile routes and readonly validation, including `uv run --no-sync pytest -q tests/test_flood_alerts_api.py tests/test_readonly_db_validation.py` when those test files exist or the repository's current equivalents otherwise.
- [ ] 5.3 Run `uv run --no-sync ruff check .`.
- [ ] 5.4 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and confirm `apps-api-layer-inversion` is zero.
- [ ] 5.5 Run `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

## 6. Issue #417 Verification

- [x] 6.1 Run `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." . -g '!apps/api/**'`.
- [x] 6.2 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [x] 6.3 Run `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

## 7. Issue #418 Verification

- [x] 7.1 Run `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." services/tiles/mvt.py` and confirm no matches.
- [x] 7.2 Run `uv run --no-sync pytest -q tests/test_flood_alerts_api.py`.
- [x] 7.3 Run `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`.
- [x] 7.4 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and confirm no `services/tiles/mvt.py` `apps-api-layer-inversion` finding remains.
- [x] 7.5 Run `uv run --no-sync ruff check services/tiles/mvt.py apps/api/routes/flood_alerts.py tests/test_flood_alerts_api.py`.
- [x] 7.6 Run `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

## 8. Issue #419 Verification

- [x] 8.1 Run `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." services/production_closure/readonly_db_validation.py` and confirm no matches.
- [x] 8.2 Run `uv run --no-sync pytest -q tests/test_readonly_db_validation.py`.
- [x] 8.3 Run `uv run --no-sync pytest -q tests/test_role_boundary_static.py tests/test_entropy_audit_script.py`.
- [x] 8.4 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and confirm zero `apps-api-layer-inversion` findings.
- [x] 8.5 Run `uv run --no-sync ruff check services/production_closure/readonly_db_validation.py apps/api tests/test_readonly_db_validation.py`.
- [x] 8.6 Run `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.
