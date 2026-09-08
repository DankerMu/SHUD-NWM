# Receipt: budget-window truncation signal (issue #2030, PR #2164)

- Date: 2026-09-08 (UTC 2026-09-08T14:39:49Z → 2026-09-08T14:41:30Z)
- Host: node-27, isolated worktree `/home/nwm/NWM-worktrees/issue2030` at PR head `6fc28eb597e28a3265b36ef19b958c126db6989e`
  (detached; the production checkout `/home/nwm/NWM` stayed at `5a86841c` serving `nhms-display-api.service` and was not
  touched). Interpreter: the production `.venv` python with `PYTHONPATH` on the worktree.
- Role: `nhms_display_ro` from `infra/env/display.env` (asserted in-script; URL never written to the repo). SELECT only.
  `NHMS_MVT_FILE_CACHE_DIR` unset and `_fetch_postgis_tile_bytes` called directly, so no cache tier was read or written.
- Method: the shipped route function `apps.api.routes.hydro_display._fetch_postgis_tile_bytes` executed in-process against
  the live database with a session proxy that hands the route the real row and keeps a copy of it, so each tile costs one
  query and yields both the route's behavior (bytes, WARNING records on logger `apps.api.routes.hydro_display`) and the
  four counters the route reads. Old-SQL bytes come from `git show 4b54e8d6:services/tiles/mvt.py` loaded as a throwaway
  module and executed with the same `_postgis_tile_params(..., layer=layer)` binds.
- Script and raw data: `.workplans/2030/receipt_2030.{py,json,log}` in the worktree (gitignored working copies).

## Verdict

| condition (tasks.md §5) | result |
|---|---|
| 5.1 forced low limit → one `MVT_TILE_BUDGET_TRUNCATED` WARNING per tile, non-empty bytes | **True** (2/2 fired) |
| 5.2 production bind, `river-network-national`, all 516 z3–z7 China tiles silent | **True** (516/516 silent; 516/516 with `intersecting == selected` on both axes; 516/516 with both overflow counts 0) |
| 5.3 bytes unchanged, old SQL vs new route | **True** (12/12 md5-equal) |
| `hydro-national q_down` 3/6/3 + 5/25/12 (measure-and-report, no silence expectation) | **3/6/3 is truncated in production today** — see §5.2b |

No `*_QUERY_VERSION` bump: bytes are identical on every sampled tile and the only SQL text change is the two projected
columns outside the `tile` sub-select (§4.4), so the cache key (which does not hash SQL, `services/tiles/mvt.py:44-49`)
keeps describing the same bytes.

## 5.1 Forced limit: the signal fires

`hydro_display.collection_coordinate_limit` monkeypatched in-process (it feeds both the `:collection_coordinate_limit`
bind and the route's `max_coordinates`), then restored and asserted restored.

| tile | bound limit | feature_count selected / intersecting | coordinate_count selected / intersecting | bytes | md5(tile) | WARNING records |
|---|---|---|---|---|---|---|
| 3/6/3 | 40000 | 23 / 56 | 38850 / 86008 | 113109 | `e370f8524045d87289132a638b5c4864` | 1 |
| 9/405/209 | 15000 | 712 / 1078 | 14983 / 22592 | 65564 | `4d2e454bb6030c45a491c1ac4e8666cf` | 1 |

Rendered records:

```text
MVT_TILE_BUDGET_TRUNCATED layer_id=river-network-national z=3 x=6 y=3 feature_count=23/56 max_features=10000 coordinate_count=38850/86008 max_coordinates=40000
MVT_TILE_BUDGET_TRUNCATED layer_id=river-network-national z=9 x=405 y=209 feature_count=712/1078 max_features=10000 coordinate_count=14983/22592 max_coordinates=15000
```

The 9/405/209 @ 15 000 digest `4d2e454bb6030c45a491c1ac4e8666cf` is byte-identical to the one the #2025 receipt recorded for
the same forced case four days earlier (`2026-09-04-national-river-density.md`, "Truncation path executed"); the 3/6/3
numbers moved slightly (38 850 / 86 008 vs 38 531 / 86 160) because the network inventory has changed since, which is the
expected behavior of a live measurement, not a determinism failure.

## 5.2 Production bind: `river-network-national` is silent on all 516 tiles

Tile set `scripts/node27_mvt_prewarm.py::xyz_tiles(CHINA_BOUNDS, [3,4,5,6,7])` = 516 tiles, bound
`collection_coordinate_limit = 120000`, `feature_limit = 10000`, `feature_coordinate_limit = 50000`.

| zoom | tiles | non-empty | max feature_count | max coordinate_count | max cold s | tiles with intersecting == selected (both axes) | WARNING records |
|---|---|---|---|---|---|---|---|
| 3 | 4 | 4 | 56 | 86008 | 2.39 | 4 | 0 |
| 4 | 9 | 6 | 33 | 52331 | 1.49 | 9 | 0 |
| 5 | 30 | 16 | 37 | 52824 | 1.42 | 30 | 0 |
| 6 | 99 | 36 | 28 | 56336 | 1.08 | 99 | 0 |
| 7 | 374 | 102 | 20 | 52381 | 0.91 | 374 | 0 |

Headroom is still read from z0: 0/0/0 measures 114225 coordinates against 120 000 (§5.3), about 5 percent; it was 114 377
on 2026-09-04. The signal this PR adds is what will tell the operator when that line is crossed.

### 5.2b `hydro-national q_down` (latest valid_time 2026-09-14T11:00:00Z): measure-and-report

| tile | valid_time | feature_count selected / intersecting | coordinate_count selected / intersecting | overflow feature / dimension | bytes | signal |
|---|---|---|---|---|---|---|
| 3/6/3 | 2026-09-14T11:00:00Z | 10000 / 10991 | 20005 / 21987 | 0 / 0 | 1847304 | **fired** |
| 5/25/12 | 2026-09-14T11:00:00Z | 7035 / 7035 | 14454 / 14454 | 0 / 0 | 1418885 | silent |

```text
MVT_TILE_BUDGET_TRUNCATED layer_id=discharge z=3 x=6 y=3 feature_count=10000/10991 max_features=10000 coordinate_count=20005/21987 max_coordinates=50000
```

**Finding (operator):** the national discharge layer's z3 tile 3/6/3 is being truncated by the fair budget window in
production today, on the *feature* arm (`:feature_limit = MVT_MAX_FEATURES = 10 000`; the coordinate total is well under
its 50 000 limit): 991 of 10 991 intersecting segments — about 9 percent — are dropped per the window's round-robin
order, and the tile still serves as a normal 200 (1.85 MB). This was previously invisible: the running production API at
`5a86841c` has zero `MVT_TILE_BUDGET_TRUNCATED` lines in `/tmp/display-api.log` because the signal did not exist. It is
exactly the class of event the issue asked to make observable; it is not a defect in this change and is routed as a
follow-up issue in PR #2164 (Phase 8). The issue's z≤8 "one row = one stream class" framing applies to
`river-network-national` only; `hydro-national` rows are per segment at every zoom, so a dropped row here is one
segment's discharge value.

## 5.3 Bytes unchanged: old SQL vs new route

| layer | tile | bytes (new route) | bytes (old SQL) | md5 | old vs new | coordinate_count selected / intersecting |
|---|---|---|---|---|---|---|
| river-network-national | 0/0/0 | 115786 | 115786 | `3ce201b22a1f0e811a46984e0617fadf` | equal | 114225 / 114225 |
| river-network-national | 1/1/0 | 204925 | 204925 | `070b47f69174e360c116fbb7baeaeee2` | equal | 114225 / 114225 |
| river-network-national | 2/3/1 | 287877 | 287877 | `fd8d8fe450230169cebceff1206b80e4` | equal | 111011 / 111011 |
| river-network-national | 3/6/3 | 248347 | 248347 | `32f2aff938739c8c972842563d47dc71` | equal | 86008 / 86008 |
| river-network-national | 4/12/6 | 149416 | 149416 | `bb6ec0fc1c4c057f1ed6352298f35bde` | equal | 52331 / 52331 |
| river-network-national | 5/25/12 | 142594 | 142594 | `8758ef14a7b4e522476e792f14bb42b4` | equal | 52824 / 52824 |
| river-network-national | 6/52/24 | 164552 | 164552 | `9a7355ff544989cf5548bfc713e1db8e` | equal | 56336 / 56336 |
| river-network-national | 7/107/46 | 117363 | 117363 | `14d917c43e9777b09d048c37faf36569` | equal | 52381 / 52381 |
| river-network-national | 9/405/209 | 100705 | 100705 | `0f71248dba97c202503cde5b1a2d503c` | equal | 22592 / 22592 |
| river-network-national | 9/404/208 | 78041 | 78041 | `49e9a4d7af01a2e04c68391d59fdb367` | equal | 18090 / 18090 |
| hydro-national | 3/6/3 | 1847304 | 1847304 | `9cf216450aa6255f8da1a2dafec92d8b` | equal | 20005 / 21987 |
| hydro-national | 5/25/12 | 1418885 | 1418885 | `bab8c1dec99eb79b7d8cba3837d6b685` | equal | 14454 / 14454 |

12/12 equal, including the three z0–z2 tiles that the fair window rescued from 413 in #2025, the two z9 per-segment tiles,
and the truncated `hydro-national` 3/6/3 (truncation itself is unchanged by this PR; only its visibility is).

## 4.4 Five-layer generated-SQL regression (local, orchestrator)

`postgis_tile_sql(layer)` text at `4b54e8d6` (master, merge base) vs PR head, `difflib.unified_diff`, all five layers:

| layer | old sha256[:16] @4b54e8d6 | new sha256[:16] | +lines | -lines |
|---|---|---|---|---|
| river-network-national | `d35ec3b9a9dd539b` | `1451726decd62178` | 2 | 0 |
| river-network | `c2b710cf13b5df6e` | `3728b8a8dac88377` | 2 | 0 |
| hydro | `83de9958f39c395b` | `bf284b1f7d6532d5` | 2 | 0 |
| hydro-national | `da706a863a85fb99` | `d18c89af633838df` | 2 | 0 |
| met-stations | `2691870627724076` | `1ed6e2f594724892` | 2 | 0 |

The two added lines are identical for every layer:

```sql
        (SELECT intersecting_feature_count FROM prefilter_stats) AS intersecting_feature_count,
        (SELECT intersecting_coordinate_count FROM prefilter_stats) AS intersecting_coordinate_count,
```

This table supersedes the five-digest anchor in `2026-09-04-national-river-density.md`. The `hydro` / `hydro-national`
"old" digests differ from that receipt's (`e9b335142768ee61` / `271b526da0921c16`) because master moved in between
(#2009/#2031 changed those two statements' run selection); `river-network-national`, `river-network` and
`met-stations` reproduce that receipt's digests exactly, so the two anchors chain.
