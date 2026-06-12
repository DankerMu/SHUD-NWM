# API Contract Frontend Consumer Evidence

Generated: 2026-06-12

Scope: Governance-5 E3 issue #414 node-27 frontend/display evidence for the #411
candidate API contracts under the #412 deprecation policy. This is evidence
only. It does not change frontend implementation, generated frontend types,
OpenAPI, backend runtime code, endpoint behavior, CI, deprecation headers, or
response metadata. OpenAPI/generated-type synchronization remains #415; removal
or explicit deferral remains #416.

## Conclusion

No node-27 frontend consumer migration is needed in #414.

The #412 policy (`docs/governance/API_CONTRACT_DEPRECATION_POLICY.md`) marks no
current endpoint deprecated or removal-ready and selects **no replacement
endpoint** for any #411 candidate. The #411 inventory
(`docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`) classifies both runtime
candidates as `active` compatibility contracts that are not removal-ready while
repository consumers remain.

Frontend/display searches confirm every current display/bootstrap/store consumer
calls the **canonical active endpoints**, not a removal-candidate replacement
(no replacement exists). Under the #412 Candidate Policy Matrix this is the
`explicit deferral` / `retain compatibility` state: consumers stay on the active
routes and nothing is migrated. This mirrors the #413 backend disposition
(`docs/governance/API_CONTRACT_BACKEND_CONSUMER_EVIDENCE.md`: "No backend code
migration is needed").

Active compatibility dependencies remain and must not be changed in #414:

- `GET /api/v1/mvp/qhh/latest-product` remains an active compatibility route. The
  display bootstrap and hydro-met popup/store consumers depend on it; there is no
  replacement to migrate to.
- `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  remains the canonical active forecast-series route. Forecast/overview stores and
  the hydro-met river-forecast helper depend on the generated canonical path.
- Docs-only shorthand `forecast-series` forms have no frontend `client.GET`
  consumer; they remain #415/#416 documentation cleanup work.

## Search Commands

`F1` latest-product frontend consumer search:

```bash
rg -n "mvp/qhh/latest-product|getQhhLatestProduct|QhhLatestProduct|fetchHydroMetLatestProduct|loadHydroMetBootstrap" apps/frontend/src
```

`F2` forecast-series frontend consumer search:

```bash
rg -n "forecast-series|/river-segments/.*forecast-series" apps/frontend/src
```

`F3` generated client call inventory (which endpoints the frontend actually calls):

```bash
rg -n "client\.GET\('/api/v1/" apps/frontend/src
```

`F4` docs-only shorthand frontend runtime search (expect none):

```bash
rg -n "/api/v1/river-segments/\{segment_id\}/forecast-series|/api/v1/river-segments/\{id\}/forecast-series|/river-segments/\{segment_id\}/forecast-series" apps/frontend/src
```

## Findings

### `GET /api/v1/mvp/qhh/latest-product`

Search `F1`/`F3` found active frontend consumers of the canonical active route:

- Direct generated calls: `apps/frontend/src/pages/hydroMet/bootstrap.ts` calls
  `client.GET('/api/v1/mvp/qhh/latest-product', ...)` twice — full bootstrap and
  `identity_only=true` popup product identity.
- Consumer fan-out: `apps/frontend/src/stores/hydroMetProductData.ts`,
  `apps/frontend/src/stores/stationLayerData.ts`,
  `apps/frontend/src/components/map/useHydroMetPopupProduct.ts`,
  `apps/frontend/src/components/map/M11RiverForecastPopup.tsx`, and
  `apps/frontend/src/components/map/M11StationForcingPopup.tsx`.
- Generated type binding: `apps/frontend/src/api/types.ts` exposes the
  `/api/v1/mvp/qhh/latest-product` path, `QhhLatestProduct` schema, and
  `getQhhLatestProduct` operation.
- Frontend test coverage: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`
  asserts direct latest-product calls and strict-identity behavior;
  `apps/frontend/src/pages/m11/__tests__/useHydroMetProduct.test.tsx` asserts
  latest-product is called once.

These are active-contract dependencies, not consumers of a removal-candidate
replacement. Under #412 there is no selected replacement for this route, so
there is no #414 frontend migration to perform.

### `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`

Search `F2`/`F3` found active frontend consumers of the canonical route:

- Generated calls: `apps/frontend/src/stores/forecast.ts`,
  `apps/frontend/src/stores/overviewData.ts` (two call sites), and
  `apps/frontend/src/lib/hydroMet/riverForecast.ts` call the generated
  `forecast-series` path.
- Generated type binding: `apps/frontend/src/api/types.ts` exposes the canonical
  `forecast-series` path entry.
- Frontend test coverage: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`
  and `apps/frontend/src/stores/__tests__/overviewData.test.ts` assert generated
  forecast-series calls.

These are active canonical-contract dependencies. #412 explicitly retains this
route and assigns documentation alignment / future deferral to #415/#416, so
these frontend consumers are not migrated in #414.

### Docs-only shorthand `forecast-series` family

Search `F4` found no frontend `client.GET` consumer of the docs-only shorthand
forms (`/api/v1/river-segments/{segment_id}/forecast-series`,
`/api/v1/river-segments/{id}/forecast-series`, and the relative variant). The
frontend uses only the generated canonical route. Shorthand cleanup or historical
marking remains #415/#416 documentation work.

## Compatibility Dependencies To Preserve

Issue #414 intentionally preserves the following frontend/display dependencies:

- Generated client usage of `/api/v1/mvp/qhh/latest-product` in
  `apps/frontend/src/pages/hydroMet/bootstrap.ts` and its store/popup fan-out.
- Generated client usage of the canonical `forecast-series` route in
  `apps/frontend/src/stores/forecast.ts`, `apps/frontend/src/stores/overviewData.ts`,
  and `apps/frontend/src/lib/hydroMet/riverForecast.ts`.
- Generated type bindings in `apps/frontend/src/api/types.ts` for both active
  contracts.
- Frontend contract/bootstrap tests that assert current active-contract behavior.

Removing or repointing any of these before #415/#416 would violate the #412
policy because active runtime routes remain compatible and no current endpoint
is deprecated or removal-ready.

## #414 Disposition

- Frontend migration needed: no.
- Frontend code changed: no.
- Generated types changed: no (synchronization is #415, gated on real migration
  evidence which does not exist under #412).
- Remaining dependency: active-contract frontend bootstrap/store/popup consumers
  continue to depend on `latest-product` and canonical `forecast-series`
  compatibility (explicit deferral per the #412 Candidate Policy Matrix).
- Acceptance verification: `cd apps/frontend && corepack pnpm run check:api-types
  && corepack pnpm build` (local + node-27 receipt); no endpoint deleted; frontend
  uses no removal-candidate endpoint because #412 designates none.
- Follow-up owner: #415 owns OpenAPI/generated-type/docs synchronization once
  migration evidence exists; #416 owns removal or explicit deferral including
  external-consumer treatment.
