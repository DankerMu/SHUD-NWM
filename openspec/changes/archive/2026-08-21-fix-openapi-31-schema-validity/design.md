## Context

Issue #1644 is an expanded/high public-schema repair. FastAPI 0.136.1 emits `openapi: 3.1.0`; `apps/api/openapi_patching.py` then injects all 109 `nullable: true` nodes. The committed YAML is an exact semantic snapshot of `app.openapi()` (`tests/test_openapi_drift.py`), so a YAML-only edit is overwritten and fails the drift oracle. With `--max-problems 1000`, the pinned Redocly 1.25.13 command reports 109 `spec`, 54 `security-defined`, and 1 `no-empty-servers` errors plus four non-failing warnings.

Fixture level: **expanded**. Repair intensity: **high** because the public API schema and auth boundary are shared entrypoints. Upstream suggested level: absent.

## Goals / Non-Goals

**Goals:**

- Publish one runtime/static OpenAPI 3.1 contract with zero invalid `nullable` keywords and unchanged null acceptance.
- Make the exact existing Redocly CI command exit zero without skip-rule/config weakening.
- Describe same-origin serving, public-by-default operations, and every currently protected operation without exposing secret values or changing enforcement.
- Preserve static/runtime equality and byte-identical generated frontend business types.
- Make OpenAPI-only and patch-owner PRs execute assertion-level drift/type tests.

**Non-Goals:**

- Do not downgrade to OpenAPI 3.0.3, change route/request/response behavior, change auth/RBAC policy, or promote internal routes.
- Do not add license metadata or the three missing 4XX warning responses; warnings remain report-only and will be routed separately.
- Do not add a schedule, change unrelated CI jobs, or require node-27/node-22 evidence.

## Decisions

### D1 — Finalize at the runtime schema owner, then regenerate the snapshot

Add one post-patch finalizer in `apps/api/openapi_patching.py`, invoked by both the direct patch pipeline and `apps/api.main`'s monkeypatch-compatible facade sequence. It recursively removes `nullable`. For the 108 ordinary typed nodes it preserves every sibling keyword and changes `type: T` to `type: [T, "null"]`; for the one `type + allOf` node it wraps the composition and `{type: "null"}` in `anyOf`. A temporary full-schema proof made Redocly green while keeping `apps/frontend/src/api/types.ts` byte-identical. Editing 109 literals or only the YAML duplicates ownership and is rejected.

### D2 — Keep OpenAPI 3.1 rather than fight the framework default

The version arose from FastAPI's runtime export and the runtime currently serves 3.1.0. Nullability is exactly representable in 3.1, so pinning/downgrading to 3.0.3 adds a compatibility knob with no user benefit. The contract test pins that the runtime and static declaration remain equal and valid.

### D3 — Satisfy Redocly with truthful server/security metadata

Publish `servers: [{url: "/", description: "Same-origin API endpoint."}]` and root `security: []`, matching the current same-origin, public-by-default API. Define conditional credential schemes without values: non-production `X-User-Role`, configured non-production bearer token, and the internal live-proof token together with required live user-id/roles headers. Exactly these 11 protected operations override root security with the three alternative credential paths:

- POST `/api/v1/basins`, `/api/v1/basins/{basin_id}/versions`, `/api/v1/river-networks`, `/api/v1/mesh-versions`, `/api/v1/models`, `/api/v1/models/{model_id}/preflight`, `/api/v1/models/{model_id}/lifecycle`, `/api/v1/river-segment-crosswalks`
- PUT `/api/v1/models/{model_id}/active`
- POST `/api/v1/runs/{run_id}/retry`, `/api/v1/runs/{run_id}/cancel`

A root-only `security: []` is forbidden because it would describe these operations as anonymous; a global bearer requirement is also false for the other 43 operations. Tests bind this set to the existing enforcement surfaces and require no secret literal in the schema.

### D4 — Self-route both artifact and owner

Add `openapi/**` to the backend paths filter and map it to `tests/test_openapi_drift.py`, `tests/test_api_contract.py`, and the new `tests/test_openapi_31_contract.py` dialect/security invariant suite. Add both drift and 3.1 invariant suites to the exact `apps/api/openapi_patching.py` rule while preserving its existing broad API consumers. Mutants that remove the filter leg or either load-bearing contract-suite leg must red.

## Risk Packs Considered

- Public API / CLI / script entry: selected — `/openapi.json` and the static contract are public entrypoints.
- Config / project setup: selected — CI paths-filter and selector routing change.
- File IO / path safety / overwrite: selected narrowly — the checked-in YAML is a regenerated artifact; no runtime user-controlled path is added.
- Schema / columns / units / field names: selected — dialect, null acceptance, server, and security shapes are the change.
- Auth / permissions / secrets: selected — security metadata must match enforcement and contain no credentials.
- Concurrency / shared state / ordering: not selected — no concurrent state or ordering changes.
- Resource limits / large input / discovery: not selected — bounded tracked schema only.
- Legacy compatibility / examples: selected — null acceptance and generated frontend types must not change.
- Error handling / rollback / partial outputs: selected — stale runtime/static/type artifacts or a lint failure must fail closed.
- Release / packaging / dependency compatibility: selected — pinned Redocly and openapi-typescript are downstream consumers; no dependency change.
- Documentation / migration notes: selected — public machine-readable API documentation changes.

All NHMS domain packs are not selected: no geospatial/CRS, hydro-met window, SHUD numerical, PostGIS/Timescale, Slurm lifecycle, external provider, run-manifest/QC, or published display identity behavior changes.

## Invariant Matrix

- Governing invariant: one runtime schema authority SHALL publish a dialect-valid OpenAPI 3.1 document whose static snapshot, null semantics, server/security truth, generated frontend business types, and CI evidence agree.
- Source of truth: `app.openapi()` after the finalizer plus the explicit protected-operation set.
- Producers: FastAPI `get_openapi`, seven patch functions, and the finalizer in `apps/api/openapi_patching.py`/`apps/api.main` facade.
- Validators/preflight: runtime/static equality, `tests/test_openapi_31_contract.py` dialect/security invariant + mutation suite, selector meta-guards, pinned Redocly, and exact-pinned openapi-typescript byte diff.
- Storage/cache/query: `api.openapi_schema`, `openapi/nhms.v1.yaml`, and `apps/frontend/src/api/types.ts`.
- Public routes/entrypoints: `/openapi.json`, static YAML consumers, Swagger/ReDoc, and CI OpenAPI Validate.
- Frontend/downstream consumers: generated `components`/`paths` types and existing stores/components importing them.
- Failure paths/stale state: missing finalizer, false anonymous/global auth, stale YAML/types, rule suppression, or selector/filter skip must red.
- Evidence/audit/readiness: focused pytest, exact lint, `check:api-types`, frontend build/test, strict OpenSpec, and SHA-bound PR CI.
- Regression rows:
  - ordinary nullable typed node -> 3.1 type union, same accepted null/value domain and identical generated type;
  - `Layer.metadata` type+allOf node -> composition-or-null without an erroneous intersection, generated type remains `LayerMetadata | null`;
  - public operation -> inherits explicit root anonymous policy; protected operation -> has truthful conditional credential alternatives and still returns existing 401/403/503 behavior;
  - OpenAPI-only PR -> backend gate opens and runs drift/type assertions plus Redocly/frontend jobs;
  - remove normalizer, protected override, server, filter/rule leg, or type parity -> deterministic red.

## Boundary Surface Checklist

- Shared helper roots: recursive schema finalizer and selector rule table.
- Public entrypoints: runtime `/openapi.json`, committed YAML, generated TypeScript.
- Read surfaces: FastAPI route/schema graph, auth-enforcement route set, YAML/type consumers.
- Write/delete/overwrite surfaces: regeneration of one tracked YAML; generated TypeScript MUST remain unchanged.
- Staging/publish/rollback: runtime and static schema land atomically in one PR; rollback reverts owner, snapshot, tests, and routing together.
- Producer/consumer evidence: runtime schema -> static YAML -> Redocly/openapi-typescript -> frontend types.
- Stale-state/idempotency: clear `app.openapi_schema` before comparison/export; finalizer is deterministic and must not double-add null.
- Unchanged downstream consumers: route behavior, auth decisions, frontend business types, API client imports.

## Risks / Trade-offs

- [Normalizer silently drops format/description/allOf] → structure tests cover all 109 nodes and byte-compare generated business types; separate composed-node mutant.
- [Security metadata lies about enforcement] → exact protected-operation matrix and root/override tests; no global-auth shortcut.
- [Static artifact drifts] → existing exact runtime/static test plus one-commit regeneration.
- [Rule suppression makes lint green] → pin the exact CI command and reject new skip/config legs.
- [New protected route later lacks metadata] → matrix test binds documented protected operations to enforcement ownership and names drift.

## Migration Plan

1. Add red tests for dialect validity, null semantic/type preservation, security/server truth, and selector self-routing.
2. Add the finalizer/security metadata at the runtime owner, regenerate YAML, and prove generated types have no diff.
3. Run focused backend/frontend checks and the exact Redocly command; push once with all artifacts.
4. Roll back by reverting owner, snapshot, tests, and routing together; never revert only the YAML.

## Open Questions

None. The four existing Redocly warnings are outside this error-only repair and are routed to #1678.
