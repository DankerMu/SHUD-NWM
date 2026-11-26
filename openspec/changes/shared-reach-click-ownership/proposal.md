# On a reach shared by two basin networks the nested network owns the click (#3390)

## Why

Where two basin river networks carry a segment on the same reach, a click on the map always opens the
segment of the same network, and nobody decided which. The click handler takes the first feature
MapLibre returns for the overlay's hit layer; with two coincident lines that is whichever was drawn last,
and the tile orders its features by ascending network id, so the network with the larger id wins.
Measured on the live display (2026-11-25): seven real clicks along `basins_byh_shud_shud_riv_000001` all
opened a segment of `basins_cj_main`; the `basins_byh` segment cannot be opened from the map at all.

Nine pairs of active networks share reaches (read-only PostGIS count on node-27, 2026-11-26, 66 active
models; output-river segments only, the ones the map draws): `basins_cj_main` with `basins_byh`
(4.9 km, one segment of `basins_byh`), `basins_hj` (53.2 km), `basins_jialingjiang` (2.2 km),
`basins_mj` (4.3 km) and `basins_wj` (2.0 km); `basins_dth_yj` with `basins_dth_zj` (177 km, 90
segments each); `basins_pearl_xj` with `basins_sw_yjhj` (306 km); and two pairs that only touch. No
pair has equal segment counts. (A first count on the issue gave four times these lengths and twice the
segments: it joined the reach rows and the output-river rows of `core.river_segment`, which hold the
same geometry twice per network.)

By the id order, the nested network (the one with fewer segments) already wins in seven of the nine
pairs: `basins_hj`, `basins_jialingjiang`, `basins_mj`, `basins_wj`, `basins_dth_zj`, `basins_sw_yjhj`
and `basins_huang_08_nlq` against `basins_hekouzhen_zhi_longmen`. It loses in two:
`basins_byh` against `basins_cj_main`, and `basins_huang_08_nlq` against
`basins_lanzhou_zhi_hekouzhen` (a touching pair). This change corrects those two and turns an accident
of naming into a rule.

The owner decided (2026-11-26): one network owns a shared reach, and it is the nested one, the network
with fewer segments. The large basin has plenty of other reaches to be opened on; a nested basin that
loses the shared reach may be unreachable on its whole main stem, and its model is the one built for
that reach.

## What changes

- The click target among the overlay hit-layer features under the pointer is chosen by an explicit
  rule instead of taken first: the feature whose network has the fewest segments; equal or unknown
  counts are decided by the network id, in the direction the map draws them today; features of one
  network keep the order the map returned them in.
- The hover highlight and the hover prefetch use the same choice, so the highlighted segment is the one
  a click opens.
- The gated river-click evidence hook and the station-locate hook already ask the product's own
  resolver; they follow the rule through it and get the same counts.
- `GET /api/v1/models` fills `segment_count` on every item. Its response model has always declared the
  field and the detail endpoint fills it, but the list query never selected it: on production all 66
  items carry `null` (read 2026-11-26), so the client has no counts at all.
- `docs/runbooks/display-mobile-evidence.md`: a pin on a shared reach is valid when it belongs to the
  owning network; the other network's pin on that reach still has to be replaced.

## Decisions

- **D1. The rule lives where the features are picked, in the frontend.** The alternative is to order
  or mark features in the tile. MapLibre's result order is draw order, a consequence of rendering and
  not a contract; a tile property would change tile bytes of four layers whose cache keys hash neither
  SQL nor binds (two version constants and, for the per-run and per-basin layers, the schema version
  would have to move, and every cached tile be regenerated). The client already loads the records
  that are meant to carry what the rule needs: the model list has `river_network_version_id` and a
  declared `segment_count`.
- **D6. The model list has to deliver the count it declares.** Found by this change's own live check:
  the first branch build, served against the running display API, changed nothing on the
  `basins_byh` reach, because every list item's `segment_count` is `null`. The list query selects
  `mi.*`, the basin id and name; the detail query additionally joins `core.river_network_version`
  and returns the count (7,971 for `basins_byh`). The list query gets the same join and column.
  `core.model_instance.river_network_version_id` is `NOT NULL` with a foreign key, so the join cannot
  drop a row. A second request per model or a new endpoint would be more code for the same number.
  One more consumer of the field becomes live with it: the overview sums it into each basin's
  `riverCount`, which feeds the `river_count` property of the basin features (no component displays
  that property today).
- **D2. "Fewer segments" is `segment_count` of the model record of the feature's
  `river_network_version_id`.** It is `core.river_network_version.segment_count`: the record count of
  the network's reach shapefile. (`core.river_segment` holds twice as many rows per network, the reach
  rows and the model's output-river rows; the ratio is the same for every network.) The overview sums
  the same field into a basin's river count. When several records name one network, the smallest
  usable count (finite, greater than zero) is used; the registry projects the count from the network
  row, so records of one network agree and this only fixes what a test may assume.
- **D3. Without counts the rule is today's order, stated.** Ties, and every case in which a network
  under the pointer has no usable count, are decided by descending `river_network_version_id`
  (code-unit comparison, no locale): the direction in which the map resolves them today. Counts are
  used only when every network under the pointer has one. So a failed or late model request, or a list
  that does not contain a network, leaves a click where it is now; in seven of the nine pairs
  the result is the same with and without counts. Two premises of "where it is now": the tile orders
  features by the database's comparison of the id, which on the production database is code-point
  order (collation `C.UTF-8`, read on node-27 on 2026-11-26), the same as the code-unit comparison
  here for these ASCII ids; and both features come from one tile. Where the two features of a point
  come from two neighbouring tiles the map's order today is not determined by the id; there the rule
  replaces an undetermined order by a determined one. A feature without a `river_network_version_id`
  sorts last (every discharge feature carries one). Within one network the rendered order is kept, so
  where only one network is under the pointer nothing changes.
- **D4. Candidates are the hit-layer features at the pointer, as today.** The hit layer is a 16 px
  wide transparent line, so "under the pointer" means within 8 px. The rule does not test whether two
  candidates are geometrically the same reach.
- **D5. One choice for click, hover and prefetch.** A highlight that shows one segment while the click
  opens another would be worse than either order.

## Accepted consequences (stated for the owner)

- **The larger network's forecast on a shared reach stays unreachable from the map.** That is option 1
  of the issue. On the two long pairs it is a lot of river (`basins_dth_yj` 177 km against
  `basins_dth_zj`, `basins_pearl_xj` 306 km against `basins_sw_yjhj`), and it is already so today:
  both are among the seven pairs the id order happens to get right. This change does not move them.
- **Within 8 px of a nested network's segment the nested network wins**, also at a confluence where
  the pointer is nearer to a different reach of the larger network.
- **Without the model records the two wrong pairs stay wrong.** The store rebuilds its snapshot, and
  clears what it derived from the models, whenever the request scope changes; a failed model request
  leaves it empty; the list is one page of at most 200 records (66 today). In those states the id
  decides, as today.

## Evidence plan

- Unit tests of the choice, the resolver, the hover path and both hooks; a wiring test that the counts
  reach the handlers from the overview's model records.
- Backend: a unit test of the list query's projection, the API contract tests, and a real-database
  integration test (CI's `real-db-integration` job; on node-27 through the throwaway instance).
- A mocked Playwright spec with a tile that holds two coincident segments of two networks and model
  records with different counts, the network with fewer segments having the smaller id: a real click
  opens the nested network's segment, in both orders of the features in the tile.
- **Live, read-only, before any deployment.** On node-27: the branch's production build in a temporary
  worktree, served on a loopback port by a static preview server; the browser script passes the page's
  `/api` requests to the running display API through a Playwright route (the preview server's own
  proxy points at a port nothing listens on there). The running API is the deployed one and its model
  list has no counts, so for the branch run the route fills each list item's `segment_count` from the
  running API's own detail endpoint for that model and passes every other field and `total` through
  untouched (read-only; this stands in for the backend half of this change until it is deployed, and
  the run without it, which has to equal the baseline, is reported next to it). The same script, in the same session, runs against
  the public site as the baseline. Points and what each can tell:

  | point | baseline (public site) | branch build | tells |
  |---|---|---|---|
  | seven points along `basins_byh_shud_shud_riv_000001` | a `basins_cj_main` segment | a `basins_byh` segment | the rule is in the build and the counts reach it (id alone gives `basins_cj_main`) |
  | one point on a `basins_cj_main` / `basins_hj` shared reach | `basins_hj` | `basins_hj` | no regression on a pair the id already gets right |
  | one point each on `basins_dth_yj` / `basins_dth_zj` and `basins_pearl_xj` / `basins_sw_yjhj` | the smaller network | the smaller network | the same, on the two long pairs |
  | `basins_qhh_shud_shud_riv_000001` (control) | itself | itself | one network under the pointer is untouched |
  | a `basins_cj_main` reach away from any nested network | `basins_cj_main` | `basins_cj_main` | the larger network is still reachable elsewhere |

  Nothing public changes; the worktree and the server are removed afterwards.
- **Deployment on node-27 is two production actions and waits for the owner's go-ahead**: restart the
  display API on the merged head (`scripts/ops/start-display-api.sh`; the list starts to carry the
  counts) and replace the built frontend (build in a temporary worktree, swap `apps/frontend/dist`
  with a backup outside the checkout, as in the earlier frontend receipts). Either order is safe: the
  old frontend ignores the counts, and the new frontend without counts behaves as today. After the
  restart: the display receipt of the bringup checklist's C1 on the merged head
  (`scripts/ops/display-c1-receipt.sh`), and an API readback that every item of
  `GET /api/v1/models?active=true&limit=200` has a `segment_count` equal to its detail endpoint's.
  After both: the same script against the public site, without the stand-in.

## Not in this change

- Tile SQL, tile properties, tile caches.
- Any use of `segment_count` other than filling the declared list field (no display of a basin's
  river count is added).
- A chooser that offers every segment under the click (option 2 of the issue).
- Hiding one network's line on a shared reach (option 3).
- Station overlap between nested basins (the station hook's `STATION_HOOK_POINT_OCCLUDED` cases).

## Impact

- Code: `apps/frontend/src/components/map/m11MapInteractions.ts`, `M11MapLibreSurface.tsx`, the
  overview data store and its snapshot type (`stores/overviewDataStore.ts`,
  `stores/overviewDataTypes.ts`; the counts become a derived snapshot field next to the existing
  `basinVersionToBasinId`), the overview page that feeds the surface; tests; the mocked e2e harness
  (tile generator, river fixture, mocks) and one spec.
- Code (backend): `packages/common/model_registry_catalog.py` (`list_models`), tests.
- Specs: `map-feature-popups` (new requirement), `m11-river-hover-highlight` (the hovered feature is
  the one a click would open), `model-activation-deactivation` (the list carries the count).
- Docs: `docs/runbooks/display-mobile-evidence.md`.
