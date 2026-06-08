## 1. Contract baseline (#358)

- [ ] 1.1 Reproduce the current CI failure locally or via GitHub logs and confirm `tests/test_api_contract.py::test_generated_frontend_types_match_openapi` is the active P0.
- [ ] 1.2 Regenerate or reconcile `apps/frontend/src/api/types.ts` from `openapi/nhms.v1.yaml` using the repository frontend toolchain.
- [ ] 1.3 Verify `uv run pytest -q tests/test_api_contract.py::test_generated_frontend_types_match_openapi`.
- [ ] 1.4 Verify `uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py`.
- [ ] 1.5 Verify `cd apps/frontend && corepack pnpm run check:api-types`.
- [ ] 1.6 Verify `cd apps/frontend && corepack pnpm test`.
- [ ] 1.7 Verify `cd apps/frontend && corepack pnpm build`.
- [ ] 1.8 If full frontend `tsc -p tsconfig.app.json --noEmit` fails for unrelated existing debt, record the failure class and do not expand #358 unless the generated API declaration shape changed.
- [ ] 1.9 Record in the PR body that Governance-1 through Governance-4 depend on this contract baseline being merged, unless a maintainer grants an explicit waiver with current failing checks listed.

## 2. Toolchain command discipline (#359)

- [ ] 2.1 Update `Makefile` targets `dev`, `migrate`, `seed-demo`, `seed-m1-model`, `test`, and `lint` to use `uv run` while preserving target names and behavior.
- [ ] 2.2 Verify or inspect the rendered commands for `make dev`, `make migrate`, `make seed-demo`, `make seed-m1-model`, `make test`, and `make lint`; document any target not executed because it would start long-running services.

## 3. Gate evidence (#353 epic)

- [ ] 3.1 Run `uv run pytest -q -m "not e2e and not grib and not integration"` or capture a CI run proving the fast gate is green.
- [ ] 3.2 Record final #353 evidence after both #358 and #359 are merged.
