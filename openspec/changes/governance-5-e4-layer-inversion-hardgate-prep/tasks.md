## 1. Import Inventory

- [ ] 1.1 Run a focused search for `apps.api` imports outside `apps/api`.
- [ ] 1.2 Confirm current `apps-api-layer-inversion` findings in `services/tiles/mvt.py` and `services/production_closure/readonly_db_validation.py`.
- [ ] 1.3 Record any additional findings as separate follow-up issues instead of expanding the implementation scope silently.

## 2. Tile Helper Boundary Fix

- [ ] 2.1 Remove `apps.api.*` imports from `services/tiles/mvt.py`.
- [ ] 2.2 Move shared tile response/error helpers to a lower-level module or adapt them in `apps/api/routes/flood_alerts.py`.
- [ ] 2.3 Run focused tile/API tests and confirm public route behavior is unchanged.

## 3. Production Closure Boundary Fix

- [ ] 3.1 Remove `apps.api.*` imports from `services/production_closure/readonly_db_validation.py`.
- [ ] 3.2 Replace the existing readonly validation API-probe exception with an API-owned adapter or injected requester; do not move FastAPI application construction or API route modules into `packages/common`.
- [ ] 3.3 Run focused readonly validation and production-closure tests.
- [ ] 3.4 Update `docs/governance/ROLE_BOUNDARY.md` so it no longer documents a stale exception after the code boundary changes.

## 4. Enforcement Prep

- [ ] 4.1 Extend static or entropy tests to prove `apps-api-layer-inversion` is zero for current code.
- [ ] 4.2 Confirm `apps-api-layer-inversion` remains a standalone role-boundary finding family and is not merged into API retirement or display cleanup.
- [ ] 4.3 Update entropy budget docs to state that layer inversion is a future hard-gate candidate only after baseline cleanup.
- [ ] 4.4 Keep `.github/workflows/governance.yml` report-only; do not enable hard-gate mode.

## 5. Verification

- [ ] 5.1 Run `uv run --no-sync pytest -q tests/test_role_boundary_static.py tests/test_entropy_audit_script.py`.
- [ ] 5.2 Run focused backend tests for tile routes and readonly validation, including `uv run --no-sync pytest -q tests/test_flood_alerts_api.py tests/test_readonly_db_validation.py` when those test files exist or the repository's current equivalents otherwise.
- [ ] 5.3 Run `uv run --no-sync ruff check .`.
- [ ] 5.4 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and confirm `apps-api-layer-inversion` is zero.
- [ ] 5.5 Run `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.
