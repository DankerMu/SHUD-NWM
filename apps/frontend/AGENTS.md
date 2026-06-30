# Frontend Agent Instructions

This file scopes root `AGENTS.md` for `apps/frontend/`. The current authority
for shared governance vocabulary is `openspec/glossary.md`; reuse terms such as
active entrypoint, legacy redirect alias, current authority, and historical
evidence exactly as the glossary defines them.

## Required Reading

- `openspec/changes/governance-7-structural-entropy-controls/specs/scoped-agent-context-governance/spec.md`
- `openspec/specs/evidence-boundary-hardening/spec.md`
- `docs/runbooks/display-readonly-live-mvt.md`
- `openspec/glossary.md`

`docs/runbooks/display-readonly-live-mvt.md` is a runbook freshness anchor for
live display MVT context. Its 2026-06-08 receipt is historical evidence: the
current authority for physical topology is node-27 active PostgreSQL on local
`:55432`, not the historical node-22 `:55433` database referenced in the old
receipt text.

## Entry Points And Route Authority

- `/` is the active entrypoint for the single-map display surface. Keep
  `OverviewPage`, the M11 map shell, and query-state handling aligned with that
  route.
- `/ops` is the current operational display path for browser proof. It remains
  RBAC-protected and must not be replaced by a mocked monitoring page when
  documenting live display evidence.
- `/overview`, `/hydro-met`, `/forecast`, `/meteorology`, `/basins/:id`, and
  `/segments/:id` are legacy redirect alias routes or
  compatibility references. Preserve redirect semantics and query propagation;
  do not recreate them as independent active display pages without a new route
  authority change.

## Map Surface Ownership

- `apps/frontend/src/pages/OverviewPage.tsx`, `apps/frontend/src/pages/m11/*`,
  and `apps/frontend/src/components/map/M11MapLibreSurface.tsx` own the M11
  single-map presentation, controls, popups, static basin/river GeoJSON display,
  and MapLibre source/layer composition.
- Data fetching, API response normalization, and frontend runtime state should
  stay in `apps/frontend/src/stores/*`, `apps/frontend/src/api/*`, and focused
  hooks. Do not hard-code basin IDs, source IDs, run IDs, MVT tile URLs, or live
  availability flags in the map surface to make a screenshot or test pass.
- Layer catalog and MVT metadata must follow the backend `/api/v1/layers` and
  API type contracts. If an API/OpenAPI contract changes, regenerate and check
  `apps/frontend/src/api/types.ts` instead of hand-editing generated shapes.
- Station-MVT remains a separate backend issue when the current authority says
  it is still open. Do not treat station overlay UI, mocked fixtures, or old MVT
  receipts as closure proof for that endpoint.

## Live Versus Mocked Evidence

- Vitest, mocked Playwright, preview, and visual lanes are regression evidence.
  They may use simulated API responses and broad `page.route('**/api/v1/**')`
  mocks only in lanes documented as mocked, preview, or visual evidence.
- Node-27 live display proof must come from the `live-display` lane with
  explicit `PLAYWRIGHT_LIVE_BASE_URL` and `PLAYWRIGHT_LIVE_API_BASE_URL`
  bindings, no broad API mocks, and a runtime config response whose
  `service_role` is `display_readonly`.
- A `live-display` receipt proves only the browser route or routes it actually
  visited and the API calls it records. Current route authority requires `/`
  plus `/ops`; if the live profile covers `/monitoring` or only a subset of the
  required routes, record the uncovered route as separate live evidence or as
  `BLOCKED`/`PARTIAL`, not inferred PASS.
- Local `pnpm test`, `pnpm build`, screenshots, and mocked route assertions do
  not satisfy node-27 live display receipts. Record missing live URLs or an
  unavailable node-27 display as `BLOCKED`, not as a mocked PASS.
- Live MVT claims must distinguish current live PostGIS MVT, static river
  GeoJSON display, 424 retry behavior, river-network low-zoom budget limits,
  and the still-separate station-MVT scope described by the runbook.

## Display Readonly Boundary

- Frontend code running against `display_readonly` may read runtime config,
  models, layers, MVT/display data, monitoring read APIs, and published
  artifacts. It must not expose retry, cancel, Slurm submission, or other
  control-plane mutations as live enabled actions.
- RBAC-denied or fail-closed control actions should stay visible only as
  disabled, blocked, or permission-denied UI states. Do not hide a forbidden
  mutation by switching the frontend to a fake role or a mocked API in live
  evidence.
- Any browser evidence that touches `/api/v1/slurm/*`, retry/cancel mutation
  endpoints, or credential-bearing URLs cannot be recorded as a live
  display_readonly PASS.

## Focused Verification

Always run the issue-required scoped-context checks after changing this file or
frontend scoped context:

```bash
(cd apps/frontend && pnpm test)
(cd apps/frontend && pnpm build)
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate --all --strict --no-interactive
```

For API contract or generated frontend type changes, also run:

```bash
(cd apps/frontend && pnpm run check:api-types)
```
