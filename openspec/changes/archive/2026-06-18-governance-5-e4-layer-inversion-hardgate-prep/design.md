## Context

Governance-1 established four role boundaries. Governance-4 made layer inversions visible through entropy reporting. The current P1 finding class is `apps-api-layer-inversion`: non-API service modules importing `apps.api.*` helpers.

These findings should be fixed as boundary work, not treated as dead code. The implementation must preserve behavior while moving shared logic to a lower layer or passing API-owned helpers into service code.

## Goals / Non-Goals

**Goals:**

- Remove `apps.api.*` imports from non-API service/shared modules.
- Keep API route behavior unchanged.
- Add tests proving the layer-inversion audit returns zero current findings.
- Make layer inversion eligible for a future hard gate after the baseline is clean.

**Non-Goals:**

- No immediate CI hard-gate enablement.
- No API route removal or OpenAPI contraction.
- No frontend changes.
- No broad refactor of unrelated service modules.

## Decisions

### D0. Stage inventory before fixes

Issue #417 is an inventory-only governance slice. It records the current
`apps-api-layer-inversion` baseline, owner area, and implementation split for
later fixes, but it does not remove imports, update role-boundary policy, or
enable hard gates.

### D1. Fix by moving shared helpers downward

If service code needs response/error constants or formatting helpers, they should live in `packages/common` or a service-owned module, not under `apps.api`.

### D2. Keep API wiring in the API layer

If a helper is API-specific, service modules should not import it. API routes should call service functions and adapt results to API responses at the boundary.

### D2a. Replace the readonly validation exception explicitly

`services/production_closure/readonly_db_validation.py` currently has a
documented exception for API smoke probes. This change must either replace that
exception with an API-owned adapter or document why the probe remains out of
gate scope. It must not move FastAPI application construction or route modules
into `packages/common`.

### D3. Split implementation by ownership

`services/tiles/mvt.py` and `services/production_closure/readonly_db_validation.py` should be separate implementation issues because they have different test surfaces and owners.

## Risks / Trade-offs

- **Risk: behavior drift in API error responses.** Mitigation: run existing route and tile tests and preserve public response contracts.
- **Risk: moving helpers creates circular imports.** Mitigation: place shared helpers in low-level modules with no API dependency.
- **Risk: hard-gate is enabled too early.** Mitigation: this change prepares future enforcement but keeps CI report-only.

## Migration Plan

1. Identify exact `apps.api.*` imports outside `apps/api`.
2. Move or duplicate only stable shared helpers into lower-level modules.
3. Update service imports and API route adapters.
4. Update `docs/governance/ROLE_BOUNDARY.md` if the existing readonly validation API-probe exception is changed.
5. Add/extend static tests for layer inversion.
6. Confirm entropy audit reports no `apps-api-layer-inversion` findings, or explicitly documents any remaining API-probe exception as not gate-eligible.

## Open Questions

- Whether production-closure readonly validation should own a local probe adapter or consume shared helpers from `packages/common`.

## Issue #417 Fixture

Fixture level: none
Repair intensity: low
Project profile: NHMS

Change surface:

- Documentation inventory for current `apps.api.*` imports outside `apps/api`.
- OpenSpec task evidence for the inventory slice.

Must preserve:

- No source import changes, runtime behavior changes, role-boundary policy
  changes, or CI hard-gate enablement in #417.
- `services/tiles/mvt.py` and
  `services/production_closure/readonly_db_validation.py` remain separate
  implementation targets for #418 and #419.

Risk packs considered:

- Public API / CLI / script entry: not selected - inventory does not change
  route, CLI, or script behavior.
- Config / project setup: not selected - no configuration or workflow change.
- File IO / path safety / overwrite: not selected - no runtime file IO change.
- Schema / columns / units / field names: not selected - no data contract change.
- Auth / permissions / secrets: not selected - no auth or credential surface.
- Concurrency / shared state / ordering: not selected - no state transition.
- Resource limits / large input / discovery: not selected - audit command reads
  repo files only and is used as evidence, not changed in #417.
- Legacy compatibility / examples: not selected - compatibility behavior is
  unchanged.
- Error handling / rollback / partial outputs: not selected - no runtime failure
  path changes.
- Release / packaging / dependency compatibility: not selected - no packaging
  or dependency change.
- Documentation / migration notes: selected - the PR adds the source-of-truth
  inventory used to stage #418/#419 without expanding scope silently.

Domain packs:

- Geospatial / CRS / basin geometry: not selected - inventory mentions tile
  ownership but does not change tile geometry behavior.
- Hydro-met time series / forcing windows: not selected - no forcing behavior.
- SHUD numerical runtime / conservation / NaN: not selected - no solver change.
- PostGIS / TimescaleDB domain behavior: not selected - no DB query change.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  behavior.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior.
- Run manifest / QC provenance: not selected - no manifest or QC change.
- Published NHMS artifacts / display identity: not selected - no artifact or
  display contract change.

Required evidence:

- Focused search:
  `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." . -g '!apps/api/**'`
  records exact import files and lines.
- Entropy evidence:
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  records current `apps-api-layer-inversion` findings.
- OpenSpec validation:
  `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

Non-goals:

- No import fixes; those belong to #418 and #419.
- No role-boundary documentation edits except inventory evidence.
- No hard-gate or workflow behavior changes.

## Issue #418 Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:

- `services/tiles/mvt.py` tile helper validation, budget, and property errors.
- `apps/api/routes/flood_alerts.py` API route adapter boundary for MVT tile
  routes and layer metadata routes that call tile helpers.
- Focused tile/API tests that assert existing public HTTP status, error code,
  details, and response headers remain stable.

Must preserve:

- Public tile route behavior for identifier validation, XYZ validation, encoded
  and raw payload byte budgets, feature/coordinate budgets, property validation,
  cache hit/miss/bypass response headers, and MVT media type.
- `services/tiles/mvt.py` stays reusable below the API layer and no longer
  imports `apps.api.*`.
- #419 readonly validation imports remain out of scope and may still be present
  after #418.

Must add/change:

- Replace service-to-API `ApiError` construction in `services/tiles/mvt.py`
  with a lower-layer tile/domain exception or equivalent shared helper.
- Adapt `apps/api/routes/flood_alerts.py` so tile/domain exceptions become the
  same `ApiError` response shape currently observed by API callers.

Risk packs considered:

- Public API / CLI / script entry: selected - public `/api/v1/tiles/**` and
  `/api/v1/layers/**` error contracts must not drift.
- Config / project setup: not selected - no env, workflow, or deployment config
  change.
- File IO / path safety / overwrite: not selected - this PR does not change
  object-store or filesystem IO; existing DB tile-cache reads/writes stay on the
  same code paths.
- Schema / columns / units / field names: selected - MVT error `details`,
  response headers, and tile metadata fields are API-visible contract fields.
- Auth / permissions / secrets: not selected - tile routes in scope do not change
  auth or credential handling.
- Concurrency / shared state / ordering: not selected - no scheduler, lock, or
  shared state transition change.
- Resource limits / large input / discovery: selected - tile byte,
  feature-count, coordinate-count, and identifier/XYZ bounds are explicit safety
  limits.
- Legacy compatibility / examples: selected - existing API tests and frontend
  route consumers expect stable tile error JSON and MVT responses.
- Error handling / rollback / partial outputs: selected - service exceptions
  must map to stable API errors without bypassing FastAPI error handlers.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - OpenSpec tasks record #418
  completion while leaving #419/#420 open.

Domain packs:

- Geospatial / CRS / basin geometry: selected - tile XYZ and MVT envelope
  behavior must remain unchanged even though SQL/geometry generation is not
  intentionally modified.
- Hydro-met time series / forcing windows: not selected - no forcing or forecast
  time-window logic changes.
- SHUD numerical runtime / conservation / NaN: not selected - no solver or
  numerical runtime change.
- PostGIS / TimescaleDB domain behavior: not selected - no SQL or DB schema
  semantics change is intended.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  surface.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider surface.
- Run manifest / QC provenance: not selected - no run manifest or QC evidence
  change.
- Published NHMS artifacts / display identity: selected - published display tile
  identity, cache keys, headers, and response bodies must remain compatible.

Required evidence:

- Focused import proof:
  `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." services/tiles/mvt.py`
  returns no matches.
- Focused tile/API tests:
  `uv run --no-sync pytest -q tests/test_flood_alerts_api.py`.
- Audit tests:
  `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`.
- Entropy audit:
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  shows no `services/tiles/mvt.py` `apps-api-layer-inversion` finding; #419
  readonly validation findings may remain.
- Static quality:
  `uv run --no-sync ruff check services/tiles/mvt.py apps/api/routes/flood_alerts.py tests/test_flood_alerts_api.py`.
- OpenSpec validation:
  `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

Non-goals:

- No readonly validation boundary fix; #419 owns
  `services/production_closure/readonly_db_validation.py`.
- No CI hard-gate enablement; #420 owns enforcement prep after #418/#419.
- No frontend changes and no API endpoint retirement.

Review focus:

- Confirm no `apps.api.*` import remains in `services/tiles/mvt.py`.
- Confirm tile/domain exceptions cannot leak as unhandled 500 errors at public
  API routes.
- Confirm HTTP status, error code, details, cache headers, and MVT response
  behavior remain covered by tests.

## Issue #419 Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:

- `services/production_closure/readonly_db_validation.py` readonly DB boundary
  validation, display route smoke, and display retry/cancel manual-action
  probes.
- API-owned adapter code that may construct FastAPI `TestClient` instances and
  depend on `apps.api.main` / `apps.api.routes.pipeline`.
- `docs/governance/ROLE_BOUNDARY.md` exception text for readonly validation
  API smoke probes.
- Focused readonly DB validation tests that cover route smoke environment,
  retry/cancel fail-closed probes, and layer-inversion scan evidence.

Must preserve:

- `validate_readonly_db_boundary()` still emits the same evidence schema,
  provenance, route smoke summaries, manual action probe records, redaction, and
  simulated-vs-live PASS blocking semantics.
- Display route smoke still runs with `NHMS_SERVICE_ROLE=display_readonly`,
  bounded readonly `DATABASE_URL`, `PGOPTIONS` readonly/timeouts, and operator
  headers.
- Manual retry/cancel probes still prove `409
  CONTROL_PLANE_MANUAL_ACTION_REQUIRED` without constructing pipeline store,
  retry service, or Slurm gateway write dependencies.
- `services/production_closure/readonly_db_validation.py` no longer imports
  `apps.api.*`; API app construction and dependency overrides are owned by an
  API-layer adapter.
- #420 hard-gate enablement remains out of scope.

Must add/change:

- Replace the service-layer `apps.api.main` / `apps.api.routes` imports with an
  injected or API-owned probe adapter boundary.
- Update `ROLE_BOUNDARY.md` so it no longer documents the old
  `readonly_db_validation.py` service-layer exception.
- Preserve the existing public helper names where callers/tests already import
  them, unless a compatibility wrapper is explicitly moved to API-owned code and
  callers are updated.

Risk packs considered:

- Public API / CLI / script entry: selected - validation scripts and runbooks
  call `validate_readonly_db_boundary()` and expect stable evidence output.
- Config / project setup: selected - the probes deliberately set safe display
  environment variables and bounded database connection settings.
- File IO / path safety / overwrite: selected - readonly validation writes
  authoritative evidence files and must not change evidence-root safety.
- Schema / columns / units / field names: selected - evidence JSON fields are a
  contract consumed by two-node evidence closure.
- Auth / permissions / secrets: selected - operator headers, readonly DB URL
  redaction, no-write dependency overrides, and readonly permission probes are
  security-sensitive.
- Concurrency / shared state / ordering: not selected - no scheduler/state
  transition behavior changes.
- Resource limits / large input / discovery: not selected - no evidence traversal
  or bounded-read logic is intentionally changed.
- Legacy compatibility / examples: selected - existing tests import route/manual
  probe helpers and should continue to pass or be updated deliberately.
- Error handling / rollback / partial outputs: selected - unexpected probe
  failures must still become structured evidence rather than uncaught crashes.
- Release / packaging / dependency compatibility: not selected - no dependency or
  packaging change.
- Documentation / migration notes: selected - role-boundary exception text must
  match the implemented boundary.

Domain packs:

- PostGIS / TimescaleDB domain behavior: selected - readonly DB permission probes
  and transaction readonly posture must remain unchanged.
- Published NHMS artifacts / display identity: selected - display route smoke
  identity evidence must remain compatible with two-node closure.
- Slurm production lifecycle / mock-vs-real parity: selected - display retry and
  cancel probes must continue to prove fail-closed behavior without reaching
  Slurm gateway dependencies.
- Run manifest / QC provenance: selected - readonly DB evidence provenance must
  still distinguish live from simulated/injected components.
- Geospatial / CRS / basin geometry: not selected - no tile geometry behavior.
- Hydro-met time series / forcing windows: not selected - no forcing behavior.
- SHUD numerical runtime / conservation / NaN: not selected - no solver behavior.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior.

Boundary-surface checklist:

- Shared/service root: `services/production_closure/readonly_db_validation.py`
  must not import `apps.api.*`.
- API-owned adapter: may import `apps.api.main`, `apps.api.routes.pipeline`, and
  `fastapi.testclient`.
- Public entrypoints: `scripts/validate_readonly_db_boundary.py` and
  `validate_readonly_db_boundary()` behavior must remain stable.
- Evidence boundary: `summary.json`, `role.json`, `route_smoke.json`, and
  `permission_probes.json` remain the authoritative files and retain redaction.
- Failure boundary: API probe failures and forbidden write-dependency
  construction remain structured `FAIL`/`BLOCKED` evidence.

Required evidence:

- Focused import proof:
  `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." services/production_closure/readonly_db_validation.py`
  returns no matches.
- Focused readonly tests:
  `uv run --no-sync pytest -q tests/test_readonly_db_validation.py`.
- Role/static and audit tests:
  `uv run --no-sync pytest -q tests/test_role_boundary_static.py tests/test_entropy_audit_script.py`.
- Entropy audit:
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  shows zero `apps-api-layer-inversion` findings.
- Static quality:
  `uv run --no-sync ruff check services/production_closure/readonly_db_validation.py apps/api tests/test_readonly_db_validation.py`.
- OpenSpec validation:
  `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

Non-goals:

- No broad production-closure refactor.
- No API endpoint retirement, OpenAPI contraction, or frontend/node-27 work.
- No CI hard-gate enablement; #420 owns enforcement semantics after the baseline
  is clean.

Review focus:

- Confirm `readonly_db_validation.py` has zero `apps.api.*` imports and no
  hidden service-to-API construction.
- Confirm live validation still uses real API route smoke/manual-action probes by
  default rather than silently downgrading to simulated or skipped evidence.
- Confirm injected test components still mark provenance as simulated and cannot
  produce live PASS evidence.
- Confirm role-boundary docs no longer preserve a stale exception.

## Issue #420 Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:

- `scripts/governance/audit_repo_entropy.py` `apps-api-layer-inversion`
  finding family and hard-gate metadata semantics, if a code change is needed.
- `tests/test_entropy_audit_script.py` and/or
  `tests/test_role_boundary_static.py` assertions that current code has zero
  layer-inversion findings after #418/#419.
- `docs/governance/entropy-budget.md`,
  `docs/governance/entropy-report.example.md`, and active OpenSpec tasks that
  describe future hard-gate eligibility and CI posture.
- `.github/workflows/governance.yml` must be inspected but not converted to
  hard-gate mode.

Must preserve:

- Default entropy audit mode remains `report-only`, exits 0 for known findings,
  and does not write `.entropy-baseline/latest.json`.
- Explicit `--mode hard-gate` remains disabled by default and counts only
  finding records whose `gate_eligible` field is true.
- Governance CI remains report-only and must not pass `--mode hard-gate`.
- `apps-api-layer-inversion` remains a standalone check ID with governance face
  `role boundary` and role `shared_contract`.
- The #418 tile and #419 readonly validation boundaries remain clean; no import
  fix, adapter refactor, API endpoint retirement, OpenAPI contraction, or
  frontend/node-27 work belongs to #420.

Must add/change:

- Add or strengthen tests that fail if the current repository again emits any
  `apps-api-layer-inversion` finding.
- Add or strengthen tests proving synthetic `apps.api.*` imports outside
  `apps/api` still emit standalone `apps-api-layer-inversion` signals under the
  role-boundary family.
- Update entropy budget documentation so layer inversion is documented as a
  future hard-gate candidate only after the zero baseline is established, while
  remaining outside current CI hard-gate enablement.
- Mark only #420 enforcement-prep tasks complete after evidence is run.

Risk packs considered:

- Public API / CLI / script entry: selected - entropy audit CLI output and exit
  semantics are automation-facing contracts.
- Config / project setup: selected - `.github/workflows/governance.yml` must
  remain report-only and not pass `--mode hard-gate`.
- File IO / path safety / overwrite: selected - audit generation must continue
  not writing `.entropy-baseline/latest.json`.
- Schema / columns / units / field names: selected - report fields
  `check_id`, `summary_counts`, `gate_eligible`, and hard-gate metadata are
  machine-readable contracts.
- Auth / permissions / secrets: not selected - no credential or auth surface.
- Concurrency / shared state / ordering: not selected - no scheduler or shared
  runtime state transition.
- Resource limits / large input / discovery: selected - audit scan coverage for
  service/shared Python files must stay bounded and deterministic.
- Legacy compatibility / examples: selected - docs/examples must distinguish
  representative report shape from live current counts.
- Error handling / rollback / partial outputs: selected - hard-gate JSON must
  remain parseable and report-only generation must stay non-failing.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - entropy budget docs carry the
  future enforcement policy.

Domain packs:

- Published NHMS artifacts / display identity: selected - the governance report
  is CI-published evidence and must not be confused with live display receipts.
- PostGIS / TimescaleDB domain behavior: not selected - no DB behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  surface.
- Run manifest / QC provenance: not selected - no run/QC evidence contract.
- Geospatial / CRS / basin geometry: not selected - no tile geometry behavior.
- Hydro-met time series / forcing windows: not selected - no forcing behavior.
- SHUD numerical runtime / conservation / NaN: not selected - no solver behavior.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior.

Boundary-surface checklist:

- Audit scan family: `apps-api-layer-inversion` is visible in
  `executed_check_families` and positive fixtures, but current repo count is
  zero.
- Hard-gate policy: current `HARD_GATE_CHECK_IDS` and docs do not make
  `apps-api-layer-inversion` a CI-enforced gate in this PR.
- Workflow policy: `.github/workflows/governance.yml` keeps report-only command
  lines and metadata assertions.
- Docs policy: examples must not imply stale current counts from before
  #418/#419 are still live.

Required evidence:

- Current audit/static tests:
  `uv run --no-sync pytest -q tests/test_role_boundary_static.py tests/test_entropy_audit_script.py`.
- Focused backend regressions:
  `uv run --no-sync pytest -q tests/test_flood_alerts_api.py tests/test_readonly_db_validation.py`.
- Static quality:
  `uv run --no-sync ruff check .`.
- Entropy audit:
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  shows zero `apps-api-layer-inversion` findings.
- Report-only workflow proof:
  inspect `.github/workflows/governance.yml` and verify no `--mode hard-gate`
  invocation is present.
- OpenSpec validation:
  `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

Non-goals:

- No CI hard-gate enablement.
- No addition of `apps-api-layer-inversion` to the currently gated CI path.
- No tile or readonly validation import fixes; #418 and #419 own those.
- No API endpoint retirement, OpenAPI contraction, or frontend/node-27 work.

Review focus:

- Confirm current repo zero baseline is tested, not only observed manually.
- Confirm synthetic layer inversions still emit the standalone finding family.
- Confirm docs make `apps-api-layer-inversion` a future candidate without
  enabling hard-gate mode or changing governance CI.
- Confirm OpenSpec tasks do not mark unrelated E3/node-27 work complete.
