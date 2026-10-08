# Tasks

## 1. Implementation

- [x] 1.1 `stationLayerData.ts`: latest-product fallback, per-context page failure isolation,
      `failedBasinIds`, `total` rule, all-failed rethrow.
- [x] 1.2 `useStationLayer.ts`: `failedBasinIds` on the model, status note.
- [x] 1.3 vitest: store cases; new hook suite `pages/m11/__tests__/useStationLayer.test.tsx` (existing notes
      pinned first).

## 2. Evidence Floor

- [ ] 2.1 Local: `cd apps/frontend && pnpm test && pnpm typecheck && pnpm build`;
      `openspec validate station-layer-per-basin-degrade --strict --no-interactive`.
- [ ] 2.2 CI green on the PR (Frontend Build).
- [ ] 2.3 node-27 after merge: frontend rebuilt/deployed, public `/?metStations=1` shows stations
      (owner's browser check).

Deviation: the degrade path has vitest evidence only (2.3 can only show that stations render). No backend change, so no node-27 real-DB receipt applies; the node-27 scratch-database
integration run still owed by PR #2717 stays deferred until the RAID link is repaired.
