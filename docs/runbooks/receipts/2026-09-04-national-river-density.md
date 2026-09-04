# Receipt: national river density v3 + fair collection budget (issue #2005, PR #2025)

- Date: 2026-09-04 (node-27 local time)
- Measured SHA: `76a08fe9bbba4bac0cdd46100ae8eb721e10cfa6` — see below. The measurement ran at
  `e1bd2547d85227673716a24d1e8f020696a980c3`, which was the PR head at the time and was rebased away
  afterwards; that hash is now a dangling object and will not resolve in a fresh clone. The surviving commit with
  the same two runtime files is `76a08fe9`, and the durable anchor is the five SQL digests below, which any reader
  can recompute.
- Baseline SHA: `1b56648b` (node-27 `/home/nwm/NWM`, master at measurement time)
- Host: node-27, `postgresql://nhms_display_ro@127.0.0.1:55432/nhms`, read-only role, SELECT only
- Method: the shipped `postgis_tile_sql("river-network-national")` executed with binds from
  `apps/api/routes/hydro_display.py::_postgis_tile_params(..., layer="river-network-national")`, so the
  binds match production by construction. The before pass ran in the master checkout, the after pass in a
  separate git worktree at the PR head, so no shared checkout was switched.
- Bound `collection_coordinate_limit`: **120000** (after) / 50000 (before)
- Validity after later commits on this branch: the measured SHA is the commit that generates the SQL.
  Commits added after it touch only tests and OpenSpec/receipt text, so
  `git diff <measured SHA> -- services/tiles/mvt.py apps/api/routes/hydro_display.py` is empty and the
  five generated layer SQL digests are unchanged (`river-network-national` = `d35ec3b9a9dd539b`,
  `river-network` = `c2b710cf13b5df6e`, `hydro` = `e9b335142768ee61`, `hydro-national` =
  `271b526da0921c16`, `met-stations` = `2691870627724076`, sha256 truncated to 16 hex).
- Bound `feature_coordinate_limit`: 50000 (unchanged, all layers)
- Tile set: `scripts/node27_mvt_prewarm.py::xyz_tiles(CHINA_BOUNDS, [3,4,5,6,7])` = 516 tiles
- Raw data: `.workplans/2005/measurements/receipt_{before,after,z9,z012_before,z012_after}.json` (gitignored working copies)

## Verdict: GO on all four conditions

| condition | result |
|---|---|
| `feature_coordinate_count < 50000` on every tile | **True** (max 19528) |
| `feature_coordinate_overflow_count == 0` on every tile | **True** |
| `coordinate_count <= 120000` on every tile | **True** (max 86160 over the specified z3-z7 set; 114377 over every tile measured, at 0/0/0) |
| production bind and unbounded (1e9) bind report equal `coordinate_count` | **True** (zero tiles truncated) |

No zoom rolls back. The v3 threshold table ships as specified.

Read the headroom from 114377, not from 86160. The specified measurement set is z3 to z7, and 86160 is its
maximum, but z0 to z2 were measured too (see below) and 0/0/0 sits at 114377 of the 120000 limit. That is the
number the residual-risk section is about.

## Per-zoom summary

| zoom | tiles | max feature_coord | overflow tiles | max coordinate_count before | after | max tile bytes | max cold seconds | truncated |
|---|---|---|---|---|---|---|---|---|
| 3 | 4 | 5828 | 0 | 42024 | 86160 | 260137 | 2.52 | 0 |
| 4 | 9 | 5828 | 0 | 27603 | 52458 | 161199 | 1.45 | 0 |
| 5 | 30 | 5454 | 0 | 27727 | 53432 | 160176 | 1.39 | 0 |
| 6 | 99 | 14395 | 0 | 28957 | 56354 | 164813 | 1.08 | 0 |
| 7 | 374 | 19528 | 0 | 26778 | 52381 | 119266 | 0.92 | 0 |

## The five hot tiles, before and after

These are the tiles that exceeded 50 000 collection coordinates under the v3 threshold table and that the
budget window plus the raised per-layer limit exist to rescue.

| tile | coordinate_count before | after | untruncated | features before -> after | bytes | cold s |
|---|---|---|---|---|---|---|
| 3/6/3 | 42024 | 86160 | 86160 | 27 -> 56 | 260137 | 2.52 |
| 4/12/6 | 27603 | 52458 | 52458 | 15 -> 32 | 161199 | 1.45 |
| 5/25/12 | 27727 | 53432 | 53432 | 18 -> 31 | 160176 | 1.39 |
| 6/52/24 | 28957 | 56354 | 56354 | 19 -> 27 | 164813 | 1.08 |
| 7/107/46 | 26778 | 52381 | 52381 | 8 -> 10 | 117363 | 0.92 |

## z0 to z2: this change repairs a live production failure

z0, z1 and z2 are reachable (`_NATIONAL_RIVER_NETWORK_METADATA` declares `min_zoom: 0`, the frontend
passes `maxzoom: 14`), share the `:z <= 4` CASE arm, and were outside the specified measured set. Measured
after the round-1 review raised them:

| tile | before coordinate_count | before bytes | after coordinate_count | after bytes |
|---|---|---|---|---|
| 0/0/0 | 52729 | 0 | 114377 | 119288 |
| 1/1/0 | 52729 | 0 | 114377 | 210915 |
| 2/3/1 | 51651 | 0 | 111163 | 298112 |

Under the previous code these tiles exceed the shared 50 000 collection budget, so the route raises
HTTP 413. Confirmed live against the running production instance on node-27 port 8080:

```
  0/0/0    -> 413 357B          3/6/3    -> 200 129690B
  1/1/0    -> 413 357B          4/12/6   -> 200  86471B
  2/3/1    -> 413 357B          5/25/12  -> 200  85207B
```

With this change all three serve. Fully zooming out currently breaks the national river layer in
production; that is fixed here as a side effect of the budget window, not as a stated goal of the issue.

## Live route check on the shipped code

A temporary single-worker instance was started from the PR worktree on port 8099 with a scratch file
cache, so neither the production API nor the other tenant on 8081 was disturbed. It was stopped
afterwards and production was re-checked green.

```
  0/0/0    -> 200 119288B 3.95s      5/25/12  -> 200 160176B 1.45s
  1/1/0    -> 200 210915B 3.45s      6/52/24  -> 200 164813B 1.14s
  2/3/1    -> 200 298112B 3.32s      7/107/46 -> 200 117363B 0.98s
  3/6/3    -> 200 260137B 2.42s      9/405/209-> 200 100705B 0.14s
  4/12/6   -> 200 161199B 1.51s
```

Every tile is 200 with a non-empty body. None is 413 and none is a silent empty 200.

## z>=9 per-segment zooms

| tile | feature_coord | coordinate_count | features | bytes | seconds | truncated |
|---|---|---|---|---|---|---|
| 9/404/208 | 50 | 18090 | 836 | 78041 | 0.21 | False |
| 9/405/208 | 27 | 20640 | 986 | 91351 | 0.10 | False |
| 9/404/209 | 57 | 24690 | 728 | 75761 | 0.13 | False |
| 9/405/209 | 28 | 22592 | 1078 | 100705 | 0.11 | False |
| 9/428/185 | 62 | 22788 | 506 | 60959 | 0.12 | False |
| 9/429/185 | 57 | 23500 | 498 | 62326 | 0.13 | False |
| 9/430/186 | 63 | 23924 | 522 | 64235 | 0.13 | False |
| 9/412/196 | 38 | 14604 | 546 | 59358 | 0.07 | False |

No z>=9 tile truncates at the shipped limit (max 24 690 against 120 000), so the spec's
"choose a truncating sample" precondition has **no valid natural sample**. Rather than record that as a
pass, the truncation path was exercised directly by binding a lower limit; see below.

## Truncation path executed, and its determinism

Binding `:collection_coordinate_limit` below a tile's own total is the only way to make the window
actually drop rows, since nothing in China reaches 120 000. Each case was generated under three forced
planner shapes: default, `max_parallel_workers_per_gather = 4` with zero parallel costs, and
`enable_indexscan`/`enable_bitmapscan` off.

| tile | bound limit | md5(tile) | coordinate_count | features |
|---|---|---|---|---|
| 9/405/209 | 15 000 | `4d2e454bb6030c45a491c1ac4e8666cf` | 14 983 | 712 |
| 9/405/209 | 40 000 | `0f71248dba97c202503cde5b1a2d503c` | 22 592 | 1 078 |
| 9/404/208 | 15 000 | `4da9bec888b87876b4ae8c17b1aa9c7f` | 14 980 | 675 |
| 9/404/208 | 40 000 | `49e9a4d7af01a2e04c68391d59fdb367` | 18 090 | 836 |
| 7/107/46 | 15 000 | `87dd0d6549925e511edcdc7b65df7158` | 13 561 | 6 |
| 7/107/46 | 40 000 | `ce057241402e4730e20e664a37ecca6a` | 26 778 | 8 |
| 3/6/3 | 15 000 | `5ea0d499b77eb17c53194c0a7033e093` | 12 925 | 9 |
| 3/6/3 | 40 000 | `a7db4e9c2f8821a5d09a65cdf8f83f9b` | 38 531 | 23 |

All three planner shapes produced the identical digest in every case: 24 generations, zero divergence.
Count them honestly: 4 distinct tiles x 2 bound limits = 8 cases, x 3 planner shapes = 24 generations.
Six of the 8 cases actually truncate. The two that do not are `9/405/209` and `9/404/208` at the 40000
bind, whose totals (22592 and 18090) are below that bind, so those two rows reproduce the untruncated
values and prove only that an untruncated tile is stable. The truncating evidence is therefore 6 cases,
of which 2 are at `z >= 9` -- the row shape where `"Type"` ties massively and the `river_segment_id`
tiebreak is the only thing pinning the cut point. That is the claim this section supports; it is not
8 tiles.

## Round-robin admission order

The spec requires that no network hold an admitted row of `network_rank = k + 1` while another network's
`network_rank = k` row was dropped. Measured on the admitted set of the forced-truncation runs:

| tile | bound limit | admitted / total rows | networks represented | highest admitted network_rank | violations |
|---|---|---|---|---|---|
| 3/6/3 | 40 000 | 23 / 56 | 23 | 1 | 0 |
| 3/6/3 | 15 000 | 9 / 56 | 9 | 1 | 0 |
| 9/405/209 | 15 000 | 712 / 1078 | 1 | 712 | 0 |

At z<=8 the merged rows mean each network contributes at most five rows, so truncation admits every
network's trunk class before any network's second class, which is exactly the intended behaviour: 23 of
23 networks keep their strongest class rather than one dense basin consuming the budget.

Note what the zero in that last column does and does not prove. In all three cases the highest admitted
`network_rank` is 1, or only one network is present, so the spec's stated violation -- a network holding an
admitted rank `k + 1` row while another network's rank `k` row was dropped -- has no opportunity to occur.
The discriminating datum is the adjacent one: at `3/6/3` bound to 40000, 23 of 23 networks each keep
exactly one row. If `network_rank` stopped leading the global order, a dense network would take several
rows and starve others, and that count would collapse.

## Notes and residual risk

- z0/0/0 measures 114 377 coordinates against the 120 000 limit, about 5 percent of headroom. The next
  activated dense network will push z0 into truncation. That degrades gracefully now rather than 413ing,
  but it is the first place truncation will appear in production.
- Truncation is silent at runtime. `budget_stats.coordinate_count` is computed after `eligible`, so the
  route can never observe that rows were dropped, and there is no log line or counter.
  `prefilter_stats.intersecting_coordinate_count` is already computed and would detect it exactly, but is
  not projected into the final SELECT. Routed as a follow-up rather than widened into this PR, because
  that column lives in the SELECT shared by all five layers.
- `NATIONAL_RIVER_COLLECTION_COORDINATE_LIMIT` is a bind value, not SQL text, and is not part of the
  cache generation. Changing it later without bumping `NATIONAL_RIVER_NETWORK_QUERY_VERSION` would serve
  stale tiles truncated at the old limit.
- z8 was not measured. It uses the merged row shape like z<=7, and the threshold table does not move
  there, but the collection-limit raise does apply.
- Cold generation at z0 to z2 is 3.3 to 4.0 seconds. Prewarm covers z3 to z5 only.

## Appendix: the per-basin layer, and why it keeps the v2 table

The go/no-go above covers `river-network-national` only. The single-basin `river-network` layer shares the
same `source_cte`, so the decision to leave it on v2 needed its own number. Measured on node-27 against the
three densest active networks, per-segment at the z7 tolerance of 200 m, assigning each segment to the tile
containing its centroid. That assignment undercounts, since a segment crossing a tile boundary is counted
once, so these are lower bounds.

| network | worst z7 tile | v2 (Type>=2) features / coords | v3 (Type>=1) features / coords |
|---|---|---|---|
| basins_shj_nj_rivnet_vbasins | 107/45 | 2 750 / 18 154 | 5 424 / 36 178 |
| basins_jialingjiang_rivnet_vbasins | 101/52 | 5 998 / 20 716 | **10 870** / 38 014 |
| basins_longmen_zhi_sanmenxia_rivnet_vbasins | 102/50 | 4 564 / 16 674 | 8 832 / 29 842 |

The binding limit for this layer is `MVT_MAX_FEATURES` (10 000), not the coordinate budget: the worst
coordinate total under v3 is 38 014, comfortably under 50 000, while Jialingjiang's feature count reaches
10 870 on one z7 tile. This is also the correction of a rationale that shipped earlier in the change and
that a review found false -- the old text claimed a dense basin already exceeded 50 000 coordinates in one
z7 tile, which the middle column disproves. The per-basin layer carries no budget window, so exceeding
`MVT_MAX_FEATURES` there is a hard failure rather than a degradation.

These numbers back the rationale at `services/tiles/mvt.py` (the `stream_type_thresholds` comment),
`specs/national-river-density/spec.md` and `design.md` D7.

## Per-tile data

The full per-tile table is machine-generated at
`docs/runbooks/receipts/2026-09-04-national-river-density.csv`, one row per measured tile with the
before and after values of `feature_coordinate_count`, `feature_coordinate_overflow_count`,
`coordinate_count`, `length(tile)`, plus the after-side untruncated `coordinate_count`, the truncation
flag, the feature count and the cold generation time. It covers the 516 China tiles at z3 to z7, the
three z0 to z2 tiles, and the eight z9 samples -- 528 rows, because z2 contributes two tiles (`2/3/1`
and the empty arctic `2/3/0`), not one.
