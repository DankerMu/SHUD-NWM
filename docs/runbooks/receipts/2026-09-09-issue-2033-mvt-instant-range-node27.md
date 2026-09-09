# Receipt: out-of-range tile instant returns 422 before any SQL (issue #2033, PR #2190)

- Date: 2026-09-09 (UTC 2026-09-09T08:53Z)
- Host: node-27. **Two isolated detached worktrees**, one commit apart, so the comparison isolates this PR's diff:
  - head arm `/home/nwm/NWM-worktrees/issue2033-head` @ `823aa4ba5ad92a6bc427d5c3af3c71a031a8051f` (PR head)
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
- Script and raw data: `/home/nwm/tmp/receipt2033/{receipt_2033.py,head.json,base.json}` on node-27 (working copies).

## Verdict

| condition (tasks.md §5) | result |
|---|---|
| 5.1 in-range instant: response identity unchanged across all six tile routes | **True** — 6/6 identical status; the one route that produces a tile is byte-, ETag-, checksum- and cache-key-identical |
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

**Why four routes answer 500 on BOTH arms — pre-existing, and already tracked as #2145.** The live database is missing
`core.river_network_version.geometry_generation`; the columns present are
`basin_version_id, checksum, created_at, river_network_version_id, river_network_version_key, segment_count, source_uri, version_label`.
That is exactly the un-applied #2031 migration #2145 is open for, and it also makes `/api/v1/layers` answer 500 on both
arms. It predates this PR, is byte-for-byte identical on both arms, and is unrelated to instant handling — but it does
mean this receipt proves **tile-byte** identity on one layer rather than five. Status/SQL-shape identity is proven on all
six routes. The remaining four layers' byte comparison is only obtainable after #2145's migration lands.

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
**not** fire on it, on either arm.

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
- **Tile-byte identity on the four #2145-blocked layers**, for the same reason. Status and SQL-shape identity are proven
  on all six routes; byte identity is proven on `river-network`.
