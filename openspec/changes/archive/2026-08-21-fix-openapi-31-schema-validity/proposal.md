## Why

`app.openapi()` and `openapi/nhms.v1.yaml` declare OpenAPI 3.1.0 but publish 109 hand-patched `nullable: true` nodes, while the exact CI Redocly command also finds 54 operations with no security declaration and a missing server declaration. The gate therefore reports 164 errors, and OpenAPI-only PRs do not run the runtime/static drift or generated-type assertion suites.

## What Changes

- Keep the runtime's OpenAPI 3.1.0 dialect and finalize every patched nullable schema into valid JSON Schema null unions without changing accepted values or generated business types.
- Publish a same-origin server, explicit anonymous-by-default root security, and truthful alternative credential requirements on the 11 operations that currently enforce auth/RBAC.
- Regenerate the static OpenAPI snapshot from `app.openapi()` while keeping committed frontend business types byte-stable.
- Route `openapi/**` and the runtime patch owner to their assertion-level contract suites, and keep the exact pinned Redocly command at zero errors without disabling rules.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `api-contract-alignment`: the published runtime/static contract is valid for its declared dialect and truthfully describes same-origin and protected-operation security boundaries.
- `ci-contract-baseline`: OpenAPI changes execute drift/type assertions and the pinned Redocly gate rather than remaining a path-filtered or collect-only blind spot.

## Impact

- Runtime schema owner: `apps/api/openapi_patching.py` and its compatibility facade in `apps/api/main.py`.
- Published/generated artifacts: `openapi/nhms.v1.yaml`; `apps/frontend/src/api/types.ts` is expected to remain byte-identical.
- Contract routing/tests: `scripts/select_ci_tests.py`, `.github/workflows/ci.yml`, `tests/test_openapi_drift.py`, `tests/test_api_contract.py`, `tests/test_openapi_31_contract.py`, and selector meta-guards.
- No route behavior, auth decision, database, Slurm/SHUD, dependency, or frontend runtime behavior changes.
