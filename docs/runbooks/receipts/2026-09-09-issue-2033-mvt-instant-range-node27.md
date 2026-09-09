# Receipt: out-of-range tile instant returns 422 before any SQL (issue #2033, PR #2190)

- Date: 2026-09-09 (UTC 2026-09-09T08:53Z)
- Host: node-27. **Two isolated detached worktrees**, one commit apart, so the comparison isolates this PR's diff:
  - head arm `/home/nwm/NWM-worktrees/issue2033-head` @ `823aa4ba5ad92a6bc427d5c3af3c71a031a8051f`. The PR head has
    since advanced to `189b43b0d3b42b36ff37c6074d7678e0413c28a5`; that delta is **tests + docs only** —
    `git diff 823aa4ba..189b43b0 -- services/tiles/mvt.py apps/api/routes/hydro_display.py apps/api/routes/precip.py`
    is empty — so every measurement below still describes the current head.
  - base arm `/home/nwm/NWM-worktrees/issue2033-base` @ `d113edca5a30bd7ca4a07a351688c3f20ff65568` (`origin/master`, the PR's base)
  The production checkout `/home/nwm/NWM` stayed at `5a86841c` serving `nhms-display-api.service` and was **not touched**
  (verified before and after). It is 165+ commits behind and is NOT a valid baseline for this comparison — hence the
  second worktree.
- Interpreter: the production `.venv` python (3.11) with `PYTHONPATH` on the respective worktree. `TMPDIR=/home/nwm/tmp`.
- Role: `nhms_display_ro` read from `infra/env/display.env` and **asserted in-script** before any query; the URL is never
  written into the repo. SELECT only.
- Method: the **shipped routes** executed in-process via `TestClient(raise_server_exceptions=False)` against the live
  database. `get_hydro_display_session` is overridden with a real `Session` on an engine carrying a
  `before_cursor_execute` listener that records **every statement's text**, not merely a count — so "zero statements"
  is checkable rather than asserted. `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`; `NHMS_MVT_FILE_CACHE_DIR` unset (no file
  cache tier on either arm). Both arms ran within 12 s of each other and discovered the **same** `valid_time`
  (`2026-09-12T22:00:00Z`) and the same display-ready `run_id`, so no cycle landed between them.
- Script and raw data: `/home/nwm/tmp/receipt2033/{receipt_2033.py,head.json,base.json}` plus the §5.1b supplement
  `{probe3_2033.py,probe3.head.json,probe3.base.json}` on node-27 (working copies).

## Verdict

| condition (tasks.md §5) | result |
|---|---|
| 5.1 in-range instant: response identity unchanged across all six tile routes | **True** — 6/6 identical status and SQL shape; **3 of the 5 layers** produce a real tile here and each is byte-, ETag-, checksum- and cache-key-identical (§5.1 + §5.1b) |
| 5.2 out-of-range instants → 422 `VALIDATION_ERROR`, `sql=0`, on both legacy routes | **True** (4/4), against base's 500 with `sql=1` / `sql=2` |
| 5.3 pre-fix public baseline recorded | **True** (below) |
| 5.4 public-URL post-fix re-measure | **Deferred to #2162** (design D6) — see "Not covered" |

## `sql=0` is measured, not assumed

The listener counts every statement the session issues, including anything session setup might run. The control row
proves the baseline is genuinely zero: `not-an-instant` — the one input already known to be rejected by FastAPI's own
path coercion before the route body — records `sql=0` on **both** arms. So the `sql=0` on the four out-of-range rows is
route-issued-statements = 0, not a counter that never fires.

## 5.1 In-range instant `2026-09-12T22:00:00Z`, tile 5/25/12, `run_id = fcst_ifs_2026090800_dg_2adca152…`

| route | base (`d113edca`) | head (`823aa4ba`) | identical |
|---|---|---|---|
| `hydro-national/{variable}` (legacy alias) | 500, sql=1 | 500, sql=1 | ✅ |
| `hydro/{run_id}/{variable}` (legacy single-run) | 413 `MVT_TILE_BUDGET_EXCEEDED`, sql=9 | 413 `MVT_TILE_BUDGET_EXCEEDED`, sql=9 | ✅ |
| `hydro-national/{source}/{cycle}` | 500, sql=1 | 500, sql=1 | ✅ |
| `river-network-national` | 500, sql=1 | 500, sql=1 | ✅ |
| `river-network/{basin_version_id}` | **200**, sql=15, 59187 B | **200**, sql=15, 59187 B | ✅ |
| `met-stations/{basin_version_id}` | 404 `MVT_SOURCE_IDENTITY_NOT_FOUND`, sql=1 | 404 …, sql=1 | ✅ |

The one route that produced a real tile is identical on every identity axis:

| axis | value (both arms) |
|---|---|
| bytes | 59187 |
| md5 | `44777f7c040078fc1319c9087190dd10` |
| ETag | `W/"m16-b455c8508621510891ed6cc58059c34c898ef392a2e180efbedce57dcf36ac27"` |
| `X-Tile-Cache-Key` | `945f1fe27e67f79c…` |
| `X-Tile-Cache` | `bypass` (RO role: no cache tier write, identical on both arms) |

**Why three routes answer 500 on BOTH arms — pre-existing, and already tracked as #2145.** The three are
`hydro-national/{variable}`, `hydro-national/{source}/{cycle}` and `river-network-national`. The live database is missing
`core.river_network_version.geometry_generation`; the columns present are
`basin_version_id, checksum, created_at, river_network_version_id, river_network_version_key, segment_count, source_uri, version_label`.
That is exactly the un-applied #2031 migration #2145 is open for, and it also makes `/api/v1/layers` answer 500 on both
arms. It predates this PR, is byte-for-byte identical on both arms, and is unrelated to instant handling.

**The other two non-tile rows above are NOT #2145 and are not blocked by it** — they are artifacts of the tile
coordinate chosen for this table, and §5.1b resolves both:

- `hydro/{run_id}/{variable}` answered **413 `MVT_TILE_BUDGET_EXCEEDED` with sql=9** — the tile SQL ran and the result
  exceeded the coordinate budget. Reaching a budget rejection proves the query executed, so this route was never
  #2145-blocked; 5/25/12 is simply a dense tile.
- `met-stations/{basin_version_id}` answered **404 `MVT_SOURCE_IDENTITY_NOT_FOUND` with sql=1** because
  `_station_source_version` found no `met.met_station` rows with `active_flag = true` for
  `basins_longyangxia_yishang_vbasins` (the basin of the discovered run). Measured:
  `SELECT basin_version_id, count(*) FILTER (WHERE active_flag) … GROUP BY 1` returns 0 active for that basin and
  1709 active for `basins_heihe_vbasins`. Also never #2145-blocked.

Both rows are still valid identity evidence as they stand — status, code and SQL count are identical on both arms.

## 5.1b Byte identity on the two routes the 5/25/12 coordinate had masked

Same method, same arms, same discovered `valid_time` and `run_id`; only the tile coordinate and the
`basin_version_id` differ. Every axis identical on both arms:

| route | tile | bytes | md5 | `X-Tile-Cache-Key` | sql |
|---|---|---|---|---|---|
| `hydro/{run_id}/q_down` @ `2026-09-12T22:00:00Z` | 7/100/50 | **90 165** | `5c4f1480b9743be9fb4283bf7f93cb82` | `b8b8aee6cc8ae3c7…` | 16 |
| `met-stations/basins_heihe_vbasins` | 3/6/3 | **66 631** | `c9bf401da20075da02637bc0ad2ac7d7` | `719ec3d2b86873c1…` | 15 |
| `met-stations/basins_heihe_vbasins` | 5/25/12 | **4 724** | `a5cc950bcb9f28459cc369385d3f6d96` | `6f1ed8e5e1ca2286…` | 15 |

ETag and `X-Tile-Checksum` are likewise byte-equal on both arms for all three
(`hydro` `W/"m16-f31ef162…"`, met-stations `W/"m16-af2ad25d…"` and `W/"m16-b1ce9328…"`), `X-Tile-Cache: bypass` on both.

The `hydro/{run_id}` row is the important one: it is a **`valid_time`-bearing** route, so its cache key and tile SQL bind
both pass through `canonical_mvt_time` on a real instant. Its identity across the arms is direct live evidence that the
in-range path is unshifted — previously that rested on the master-recomputed digest literals plus inspection alone,
because the only live tile-producing route measured (`river-network`) carries `valid_time = None`.

Nine further `hydro/{run_id}` coordinates (z6 50/24, 51/24, 51/25; z7 101/50, 102/50, 103/51; z8 201/100, 203/101) return
an empty 200 tile on both arms, and z6 50/25 returns 413 on both arms — all identical, none used as byte evidence.

**Net: tile-byte/ETag/cache-key identity is proven on 3 of the 5 layers** (`river-network`, `hydro`, `met-stations`).
The remaining two — `hydro-national` (both its routes) and `river-network-national` — are the genuinely #2145-blocked
ones, and only their byte comparison waits on that migration.

## 5.2 Out-of-range instants: the fix

| route | instant | base (`d113edca`) | head (`823aa4ba`) |
|---|---|---|---|
| `hydro-national/q_down` | `9999-12-31T23:59:59-08:00` | **500**, sql=**1** | **422** `VALIDATION_ERROR`, sql=**0** |
| `hydro/{run_id}/q_down` | `9999-12-31T23:59:59-08:00` | **500**, sql=**2** | **422** `VALIDATION_ERROR`, sql=**0** |
| `hydro-national/q_down` | `0001-01-01T00:00:00+08:00` | **500**, sql=**1** | **422** `VALIDATION_ERROR`, sql=**0** |
| `hydro/{run_id}/q_down` | `0001-01-01T00:00:00+08:00` | **500**, sql=**2** | **422** `VALIDATION_ERROR`, sql=**0** |

The base arm's `sql=1` (national) and `sql=2` (single-run) reproduce the issue's measured amplification **exactly**, on
the live database. The head arm's error body:

```json
{"status":"error","error":{"code":"VALIDATION_ERROR",
 "message":"Tile time instants must be representable in UTC.",
 "details":{"valid_time":"9999-12-31T23:59:59-08:00","expected_format":"YYYY-MM-DDTHH:MM:SSZ"}}}
```

Control rows, identical on both arms:

| control | base | head |
|---|---|---|
| `not-an-instant` (malformed → FastAPI coercion) | 422, sql=0 | 422, sql=0 |
| naive `9999-12-31T23:59:59` (must **not** be 422 — naive reads as UTC and cannot overflow) | 500, sql=1 (the #2145 500) | 500, sql=1 (the #2145 500) |

The naive control is the live counterpart of the unit test that locks the ternary's naive branch: the new guard does
**not** fire on it, on either arm. The live evidence here is `sql=1` + non-422 on both arms, which discriminates against
the failure mode (a 422 would have shown `sql=0`); it is weaker than the unit test's `== 200`, because on this database
the naive control lands on the #2145 500 regardless of arm. The `== 200` half is fixture-only until #2145 lands.

## 5.3 Pre-fix public baseline

Measured against `https://test.nwm.ac.cn` on 2026-09-08, before this PR existed (the reverse proxy fronts the parked
production checkout):

| URL instant | status |
|---|---|
| `not-an-instant` | 422 |
| `9999-12-31T23:59:59-08:00` | **500** |
| `0001-01-01T00:00:00+08:00` | **500** |

## Not covered by this receipt

- **Public-URL post-fix re-measure** (issue criterion 6, second half). `nhms-display-api.service` serves
  `/home/nwm/NWM`, parked on `hotfix/node27-rollback-pre-2073` = `5a86841c`, 165+ commits behind; restoring it is an ops
  decision tracked by **#2145** (the blocking migration) and **#2162** (the blocked deployment). Routed there; not
  silently dropped.
- **Tile-byte identity on the two genuinely #2145-blocked layers** (`hydro-national`, both routes, and
  `river-network-national`), for the same reason. Status and SQL-shape identity are proven on all six routes; byte,
  ETag, checksum and cache-key identity are proven on 3 of the 5 layers — `river-network` (§5.1), `hydro` and
  `met-stations` (§5.1b).
