# Tasks: shared-reach-click-ownership (#3390)

Fixture level: `standard`. Risk packs: user-visible map interaction; evidence-hook contract.

## Must preserve

- With one network under the pointer the chosen feature is the first the map returned, exactly as today.
- Station cluster and station point win over the overlay; the basin fill loses to it.
- `onMapClick` fires on every click; `onOverlayClick` receives the map's own feature object, unmodified.
- The hover tracker updates only when the hovered id changes; pointer cursor and latest-product
  prefetch behave as today.
- The evidence hooks expose the same methods and closed error codes; without
  `window.__NHMS_E2E_HOOKS__ === true` they are absent.
- No tile, API or cache change; no new request.
- The existing mocked e2e tile fixture and spec are unchanged.
- When the model records are missing or failed, every click resolves as it does today.

## 1. Fixture

- [x] 1.1 Fixture review (`pass`) and `openspec validate shared-reach-click-ownership --strict --no-interactive`.

## 2. The choice

- [ ] 2.1 `apps/frontend/src/components/map/m11MapInteractions.ts`: one pure function that orders the
      hit-layer features under the pointer by the requirement's four rules and returns the first; no
      map, no DOM, no locale-dependent comparison; the input array is not mutated.
- [ ] 2.2 `resolveM11ClickTarget` takes the per-network segment counts and uses 2.1 for the overlay
      step; the other steps are untouched. Every production caller passes the counts (click handler,
      river-click hook adapter, station-locate hook adapter).
- [ ] 2.3 `handleM11MapMouseMove` uses the same choice for the overlay feature it hands to
      `onOverlayHover`.

## 3. Wiring

- [ ] 3.1 The counts are a derived field of the overview data snapshot, built by a pure function from
      the model records the store already loads (`river_network_version_id` to the smallest usable
      `segment_count`; records without a network id or usable count contribute nothing), next to the
      existing `basinVersionToBasinId`, and cleared and rebuilt with it. No new fetch.
- [ ] 3.2 They reach `M11MapLibreSurface` as a prop; the two hook adapters read them through a ref, so
      that a hook installed before the records arrived resolves with them afterwards.

## 4. Tests

- [ ] 4.1 One test per scenario of the ADDED requirement and for the new hover scenario, each asserted
      in both orders of the map's result where the scenario says "either order". In the owner scenario
      the network with fewer segments has the smaller id, so an implementation that ignores counts
      fails it.
- [ ] 4.2 The pure function: counts 0, negative, NaN, Infinity, missing (each switches the counts off
      for the whole candidate set); a feature without a network id; three networks; many features of
      one network keep their order; code-unit comparison (a pair of ids that a locale-aware comparison
      orders the other way); the input is not mutated.
- [ ] 4.3 The existing first-wins tests (`m11MapInteractions.test.ts`, the hook tests) are kept for
      the one-network case and gain the two-network case; none is deleted. List each changed test with
      what it proves now.
- [ ] 4.4 Wiring: the derivation (several records of one network, unusable counts, cleared with the
      snapshot); a test at the surface or page level that a click with two coincident features and
      loaded model records opens the nested network's segment; the river-click hook's resolver sees
      counts that arrived after the hook was installed; the station-locate adapter passes the counts
      (asserted on the call's argument: that hook accepts only a station target, so the overlay order
      cannot change its result).
- [ ] 4.5 Red runs, each restored: the choice replaced by "first"; counts ignored (id only); the count
      comparison inverted; the id order inverted; counts used although one network has none; unknown
      counts treated as zero; hover left on "first"; a hook adapter not given the counts; a hook
      adapter capturing the counts at install instead of reading the ref; the derivation taking the
      largest count.
- [ ] 4.6 Mocked Playwright (`apps/frontend/e2e`):
      - the tile generator emits an ordered list of features from a second fixture description; the
        existing tile fixture stays byte-identical (`cmp`) and the existing spec is untouched;
      - two new tile fixtures hold the same two coincident segments of two networks in the two
        orders; the mocks gain the second model record, basin and forecast identity (different
        `segment_count`, the network with fewer segments having the smaller id) behind an opt-in
        argument of the mock installer that only the new spec passes, so that the responses the
        existing specs receive are unchanged;
      - the new spec adds no DOM attribute for "counts loaded": it polls the hook (`expect.poll`,
        bounded) asking for the larger network's segment until the answer is `HOOK_POINT_OCCLUDED`
        (before the counts arrive the id decides and that call resolves); then the hook asked for the
        nested segment resolves, and a real pointer click at that point opens the river window titled
        with the nested segment; with both fixtures;
      - red run: with the choice replaced by "first", exactly one of the two fixtures fails (this
        proves the two fixtures really present the two orders).

## 5. Docs (orchestrator)

- [ ] 5.1 `docs/runbooks/display-mobile-evidence.md`: a pin on a shared reach is valid when it belongs
      to the owning network; what `HOOK_POINT_OCCLUDED` means there now.

## 6. Evidence Floor

- [ ] 6.1 `corepack pnpm test` for the touched test files, `tsc`, lint and `pnpm build` pass locally;
      the mocked spec of 4.6 passes locally.
- [ ] 6.2 node-27, live and read-only, before any deployment: the table of the proposal's evidence
      plan, baseline (public site) and branch build (loopback preview, `/api` passed to the running
      display API by a Playwright route) in one session; table in the PR.
- [ ] 6.3 Code review (two seats: interaction correctness; test evidence); CI green including every
      mocked e2e shard.

After the merge, not a task of this change: the frontend deployment on node-27 waits for the owner's
go-ahead; once deployed, the same script runs against the public site and the receipt goes on the issue.
