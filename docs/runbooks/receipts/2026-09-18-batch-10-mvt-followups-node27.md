# Receipt: batch 10 MVT follow-ups (#2156 #2154 #2157 #2166 #2165 #2160)

- Date: 2026-09-18 (UTC)
- Host: node-27. Independent `git clone --shared` of `/home/nwm/NWM` at `/home/nwm/tmp/b10-int`, detached at PR head
  `75a7a84481323867497ae9c250274620a970f921`; the production checkout `/home/nwm/NWM` and `nhms-display-api.service`
  were not touched. Interpreter: a dev venv (`uv sync --all-extras --dev --frozen`) at `/home/nwm/tmp/b10-venv`, with
  `PYTHONPATH` on the clone.
- Production reads: role `nhms_display_ro` (asserted in-script; URL read from the node-local `infra/env/display.env`
  and never written anywhere), every tile in a `SET TRANSACTION READ ONLY` transaction rolled back afterwards.
  `NHMS_MVT_FILE_CACHE_DIR` unset and `_fetch_postgis_tile_bytes` called directly, so no cache tier was read or written.
- Real-DB tests: owner role against a throwaway `nhms_it_<uuid>` database per test (`tests/conftest.py`), never `nhms`.
- Script and raw data: `.workplans/pr-b10/receipt_b10.{py,json,log}` (gitignored working copies).

## Verdict

| tasks.md D-3 | result |
|---|---|
| (a) `core.river_network_version.geometry_generation` exists (000057 applied) | **True** (`information_schema` count 1) |
| (b) `hydro-national q_down` legacy route, latest valid_time `2026-09-24T11:00Z`, all China z0–z5 (47 tiles): zero `MVT_TILE_BUDGET_TRUNCATED`, zero 413 | **True** (47/47 HTTP 200, selected = intersecting on every tile) |
| (c) latest complete gfs cycle `2026-09-17T12Z` (valid_time = window start), z0–z3 (8 tiles): zero truncation | **True** (8/8 HTTP 200, selected = intersecting) |
| (d) `river-network-national` z3–z7, 516 tiles: zero TRUNCATED, zero BLANKED | **True** (516/516 silent, 0 overflow) |
| (e) forced `feature_coordinate_limit = 100` on `river-network-national` 3/6/3: one BLANKED record, zero-feature tile | **True** |
| (f) one-time cache-key rotation | recorded below |

## (b) Legacy route, production bind (`feature_limit = 20000`, version `fair-network-budget-v6`)

| zoom | tiles | non-empty | max selected / intersecting features | max bytes | max cold s | TRUNCATED | BLANKED |
|---|---|---|---|---|---|---|---|
| 0 | 1 | 1 | 13 770 / 13 770 | 48 881 | 3.29 | 0 | 0 |
| 1 | 1 | 1 | 13 770 / 13 770 | 787 925 | 2.73 | 0 | 0 |
| 2 | 2 | 2 | 13 392 / 13 392 | 2 143 572 | 2.79 | 0 | 0 |
| 3 | 4 | 4 | 10 991 / 10 991 | 2 043 786 | 2.30 | 0 | 0 |
| 4 | 9 | 6 | 7 242 / 7 242 | 1 374 324 | 1.62 | 0 | 0 |
| 5 | 30 | 16 | 7 035 / 7 035 | 1 418 908 | 1.50 | 0 | 0 |

Coordinates: maximum 27 576 (z0/z1), against the layer's 50 000 collection budget. Largest tile 2.14 MB against
`MVT_MAX_BYTES` 5 000 000.

Old bytes = the same tile SQL (its text is unchanged by this PR) bound with the pre-PR `feature_limit = 10000`;
new bytes = the shipped route. "warm" is an immediate second execution of the same query (no cache tier; PostgreSQL
buffers warm), not an HTTP cache hit.

| tile | old bytes | old md5 | new bytes | new md5 | features | coords | cold s | warm s |
|---|---|---|---|---|---|---|---|---|
| 0/0/0 | 42 800 | `e60ae1846512aa7d540536dab5cf638f` | 48 881 | `01a1f919ad4158b61886f3f68fbe253d` | 13 770 | 27 576 | 3.29 | 2.80 |
| 1/1/0 | 635 091 | `265198da6f1074ae73a58aa32b922ee0` | 787 925 | `1bac3cdaff07757e17001e1b8d07df37` | 13 770 | 27 576 | 2.73 | 2.74 |
| 2/2/1 | 47 894 | `8d957c8742a4f998022ffe651825aae5` | 47 894 | equal | 378 | 758 | 0.40 | 0.37 |
| 2/3/1 | 1 598 418 | `809fa6b47aca3a912626b94e7a7d8279` | 2 143 572 | `dda80f7cc2a2a8862d708279aa79f6f8` | 13 392 | 26 818 | 2.79 | 2.77 |
| 3/5/2 | 15 540 | `f907da23e1420de19380329051c762d9` | 15 540 | equal | 93 | 188 | 0.34 | 0.34 |
| 3/5/3 | 45 088 | `0520e256360b6e185468bb801327b215` | 45 088 | equal | 285 | 570 | 0.35 | 0.36 |
| 3/6/2 | 398 948 | `c40f42814bfa755eee4692023cffafef` | 398 948 | equal | 2 406 | 4 841 | 0.84 | 0.83 |
| 3/6/3 | 1 847 138 | `683bc60100b529c2482afe1fb6ed0020` | 2 043 786 | `fd271597c86ae1c92869b26f523bcb75` | 10 991 | 21 987 | 2.30 | 2.30 |

Exactly the four tiles that intersect more than 10 000 features changed bytes; every tile under the old limit is
md5-identical. That is the reason `NATIONAL_DISCHARGE_QUERY_VERSION` moves to v6 (the cache key hashes neither SQL
nor binds).

## (c) Canonical per-cycle route (`gfs`, cycle `2026-09-17T12:00Z`, valid_time `2026-09-17T12:00Z`)

| tile | status | bytes | selected / intersecting | coords | cold s |
|---|---|---|---|---|---|
| 0/0/0 | 200 | 49 020 | 13 770 / 13 770 | 27 576 | 2.72 |
| 1/1/0 | 200 | 787 759 | 13 770 / 13 770 | 27 576 | 2.78 |
| 2/2/1 | 200 | 48 115 | 378 / 378 | 758 | 0.37 |
| 2/3/1 | 200 | 2 143 365 | 13 392 / 13 392 | 26 818 | 2.81 |
| 3/5/2 | 200 | 15 540 | 93 / 93 | 188 | 0.33 |
| 3/5/3 | 200 | 45 294 | 285 / 285 | 570 | 0.35 |
| 3/6/2 | 200 | 398 971 | 2 406 / 2 406 | 4 841 | 0.82 |
| 3/6/3 | 200 | 2 043 571 | 10 991 / 10 991 | 21 987 | 2.29 |

Zero `MVT_TILE_BUDGET_TRUNCATED`, zero `MVT_TILE_FEATURE_OVERFLOW_BLANKED`.

## (d) `river-network-national` z3–z7 (516 tiles, `CHINA_BOUNDS`)

| zoom | tiles | non-empty | max features | max bytes | TRUNCATED | BLANKED |
|---|---|---|---|---|---|---|
| 3 | 4 | 4 | 56 | 248 347 | 0 | 0 |
| 4 | 9 | 6 | 33 | 149 416 | 0 | 0 |
| 5 | 30 | 16 | 37 | 148 784 | 0 | 0 |
| 6 | 99 | 36 | 28 | 164 552 | 0 | 0 |
| 7 | 374 | 102 | 20 | 119 266 | 0 | 0 |

Both overflow counters are 0 on all 516 tiles; the feature limit of this layer stays `MVT_MAX_FEATURES`.

## (e) Forced per-feature overflow

`hydro_display.MVT_MAX_COORDINATES` monkeypatched in-process to 100 (it feeds `:feature_coordinate_limit`), then
restored and asserted restored. `river-network-national` 3/6/3 → HTTP 200, **0 bytes**, one record:

```text
MVT_TILE_FEATURE_OVERFLOW_BLANKED layer_id=river-network-national z=3 x=6 y=3 feature_coordinate_overflow_count=51 feature_coordinate_count=4962 max_feature_coordinates=100 coordinate_dimension_overflow_count=0 coordinate_dimension_count=2 max_coordinate_dimensions=3
```

The dimension-overflow arm cannot be forced on real 2D data; its oracle is the stub-row unit test
(`tests/test_hydro_display_mvt_scaling.py::test_coordinate_dimension_overflow_blanks_the_tile_observably`).

## (f) Cache-key rotation on deploy (expected, one cold miss each)

- `hydro-national` (both routes): `NATIONAL_DISCHARGE_QUERY_VERSION` v5 → v6.
- per-basin `river-network`: the digest basis gains `segment_count|checksum|geometry_generation` per network.
- run-scoped `hydro`: `_run_source_version`'s revision basis gains `geometry_generation`.
- Deploy dependency: the per-basin `river-network` and run-scoped `hydro` routes now read
  `core.river_network_version.geometry_generation` (000057), which (a) shows is applied on node-27.

## Real-DB test lane

The selector's 68 test files for this PR (including every integration file) ran on the same clone at `33b999d8a` (the head before the guard-revert commit):
`4757 passed, 1 failed, 4 skipped in 1887.98s`. The single failure was the #1948 structural-guard contract rejecting
a `.large-file-guard.json` exclusion this branch had added; the exclusion was removed (the guard file is identical to
master) and that group re-ran green.
