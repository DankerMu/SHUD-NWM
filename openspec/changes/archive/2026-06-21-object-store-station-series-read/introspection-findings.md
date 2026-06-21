# Object-store Station Series Read - Introspection Findings

Date: 2026-06-21
Scope: pre-implementation introspection only. No reader implementation or reader tests were added.

## Summary

The node-27 object-store forcing layout is readable by the `nwm` display user and contains 10 available cycle directories per source (`ifs`, `gfs`), for 20 source-cycle directories total. All cycle directory names are 10-digit `YYYYMMDDHH`.

The sampled SHUD CSV structure is stable in column shape but not in row count across sources:

- `ifs`: header row 1 is `53 6 <YYYYMMDD> <YYYYMMDD+7d>`, data rows = 53.
- `gfs`: header row 1 is `56 6 <YYYYMMDD> <YYYYMMDD+7d>`, data rows = 56.
- Both sources use header row 2 exactly: `Time_Day Precip Temp RH Wind RN`.
- `heihe` has 1709 SHUD CSV files per sampled cycle; `qhh` has 386 SHUD CSV files per sampled cycle.

This updates the earlier "observed 53 rows" assumption: the reader must rely on the `nrow` header and must not hardcode 53.

## 0.1 OQ1 - CSV Header And Row Count Scan

Status: done.

Remote command shape:

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
.venv/bin/python -  # read-only script over /home/ghdc/nwm/object-store/forcing
```

Note: `uv` is not present in node-27 PATH, so the existing project virtualenv interpreter was used for read-only inspection only. No remote files were modified.

Coverage:

- Sources: `ifs`, `gfs`
- Basins: `heihe`, `qhh`
- Available cycles: 10 per source, 20 source-cycle directories total
- Sample: first 5 sorted `shud/*.csv` files per source-cycle-basin combo
- Total sampled files: 200

Observed cycles per source:

```text
ifs: 2026061600, 2026061612, 2026061700, 2026061712, 2026061800,
     2026061812, 2026061900, 2026061912, 2026062000, 2026062012
gfs: 2026061600, 2026061612, 2026061700, 2026061712, 2026061800,
     2026061812, 2026061900, 2026061912, 2026062000, 2026062012
```

Representative samples:

```text
ifs/heihe/2026061600/X100.05Y38.05.csv:
  53	6	20260616	20260623
  Time_Day	Precip	Temp	RH	Wind	RN
  data rows: 53

ifs/qhh/2026061600/X100.05Y36.45.csv:
  53	6	20260616	20260623
  Time_Day	Precip	Temp	RH	Wind	RN
  data rows: 53

gfs/heihe/2026061600/X100.05Y38.05.csv:
  56	6	20260616	20260623
  Time_Day	Precip	Temp	RH	Wind	RN
  data rows: 56

gfs/qhh/2026061600/X100.05Y36.45.csv:
  56	6	20260616	20260623
  Time_Day	Precip	Temp	RH	Wind	RN
  data rows: 56
```

Aggregated evidence:

- Sampled row count range: min 53, max 56, unique `[53, 56]`.
- `nrow` matched the physical data row count in every sampled file.
- `ncol` was always 6.
- File count range by source-cycle-basin combo: min 386, max 1709, unique `[386, 1709]`.
- Header row 2 was stable across all 200 sampled files.

Implementation implication:

- Parser must treat row count as source/cycle data, not a fixed constant.
- Tests should include both 53-row and 56-row cases, plus the planned N=1/N=100 synthetic cases.

## 0.2 OQ2 - Cycle Directory Name Format

Status: done.

Remote command shape:

```bash
find /home/ghdc/nwm/object-store/forcing -maxdepth 2 -mindepth 2 -type d
```

Evidence:

- `/home/ghdc/nwm/object-store/forcing/ifs/`: 10 directories, all match `^[0-9]{10}$`.
- `/home/ghdc/nwm/object-store/forcing/gfs/`: 10 directories, all match `^[0-9]{10}$`.
- All observed directory names are `YYYYMMDDHH`.

## 0.3 OQ3 - `met_station.basin_version_id` Matches Disk Basin Directories

Status: done.

DB access method:

- Read `infra/env/display.env` on node-27 only for key presence.
- `DATABASE_URL` was present.
- `OBJECT_STORE_ROOT` was not present in current `display.env`.
- No full environment values or secrets were printed.
- Query used a read-only session via the existing project virtualenv.

Query shape:

```sql
SELECT
  basin_version_id,
  count(*),
  count(properties_json->>'forcing_filename'),
  count(DISTINCT properties_json->>'forcing_filename'),
  min(station_id),
  max(station_id),
  bool_and((properties_json->>'forcing_filename') ~ '^X[-0-9.]+Y[-0-9.]+\.csv$')
FROM met.met_station
WHERE basin_version_id IN ('basins_heihe_vbasins', 'basins_qhh_vbasins')
GROUP BY basin_version_id
ORDER BY basin_version_id;
```

Results:

| basin_version_id | station_count | forcing_filename_count | distinct_forcing_filename_count | filename shape |
|---|---:|---:|---:|---|
| `basins_heihe_vbasins` | 1709 | 1709 | 1709 | ok |
| `basins_qhh_vbasins` | 386 | 386 | 386 | ok |

Disk basin-version directories found under `/home/ghdc/nwm/object-store/forcing/*/*/`:

```text
basins_heihe_vbasins
basins_qhh_vbasins
```

Conclusion: DB basin version ids for heihe/qhh match the disk `{basin_version_id}` path segment 100%.

## 0.4 OQ4 - OBJECT_STORE_ROOT Read Permission Is Sufficient

Status: done.

Remote user:

```text
user: nwm
groups: nwm sudo docker
```

Permission evidence:

| path | mode | read | execute | write |
|---|---|---|---|---|
| `/home/ghdc/nwm/object-store` | `drwxrwxr-x` | yes | yes | no |
| `/home/ghdc/nwm/object-store/forcing` | `drwxr-xr-x` | yes | yes | no |
| `/home/ghdc/nwm/object-store/forcing/ifs` | `drwxr-xr-x` | yes | yes | no |
| `/home/ghdc/nwm/object-store/forcing/ifs/2026062012` | `drwxr-xr-x` | yes | yes | no |
| `/home/ghdc/nwm/object-store/forcing/ifs/2026062012/basins_heihe_vbasins/basins_heihe_shud/shud` | `drwxr-xr-x` | yes | yes | no |

Sample file:

```text
/home/ghdc/nwm/object-store/forcing/ifs/2026062012/basins_heihe_vbasins/basins_heihe_shud/shud/X100.75Y37.65.csv
mode: -rw-r--r--
size: 3422 bytes
first rows:
  53	6	20260620	20260627
  Time_Day	Precip	Temp	RH	Wind	RN
```

Conclusion: the display-side reader only needs directory read+execute and file read permission. Current node-27 permissions already allow read traversal while denying write to the `nwm` user, which supports the read-only boundary.

## 0.5 OQ5 - Injection Pattern Decision

Status: done.

Local code inspected:

- `apps/api/main.py:create_app()` stores `runtime_config` on `api.state.runtime_config`.
- `apps/api/main.py:/api/v1/runtime/config` reads from `request.app.state.runtime_config`.
- `apps/api/routes/data_sources.py` and sibling route modules use FastAPI `Depends(get_...)` provider functions for stores.
- Existing route tests override dependency providers; app-global values are best kept on `app.state`.

Decision:

- Store the startup-validated root at `app.state.object_store_root` in `create_app()`.
- Expose it to the route through a small `Depends(get_object_store_root)` provider that reads `request.app.state.object_store_root`.
- Provide station lookup through `Depends(get_station_lookup)`, returning a `PsycopgStationLookup` instance.

Rationale:

- `object_store_root` is app-scoped immutable runtime config, so `app.state` is the right storage location.
- Route dependencies should still use `Depends(...)` so tests can override `get_object_store_root` and `get_station_lookup` without mutating global state.
- This aligns with AD-12's FastAPI Depends direction and refines AD-7's "app.state or Depends" option into both: state for storage, Depends for injection.

## 0.6 Test Layout Decision

Status: done.

Decision: use flat test files under `tests/`, specifically `tests/test_object_store_forcing.py` for PR-A reader unit coverage. Do not create `tests/packages/common/`.

Rationale: existing common/package behavior tests are mostly flat (`tests/test_forecast_api.py`, `tests/test_list_search_contract.py`, `tests/test_forecast_store_product_quality_sql.py`, etc.), so a flat layout matches local style.

## 0.7 Findings Persistence

Status: partially done by this leaf task.

This file records the introspection results in the repository:

```text
openspec/changes/object-store-station-series-read/introspection-findings.md
```

The task text says the findings must be committed before section 1 opens, but this leaf task is under a "do not commit" boundary. Commit is therefore deferred to the parent orchestrator.

## 0.8 OQ6 - `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` Source

Status: done.

Actual source:

```text
scripts/validate_two_node_docker_runtime.py
```

Relevant constants:

- `DISPLAY_FORBIDDEN_ENV_KEYS` includes `OBJECT_STORE_ROOT`.
- `COMPUTE_ONLY_PATH_ENV_KEYS` also includes `OBJECT_STORE_ROOT`.
- `DISPLAY_REQUIRED_RUNTIME_ENV` currently does not include `OBJECT_STORE_ROOT`.

Coupling found:

- `tests/test_role_boundary_static.py` imports `scripts.validate_two_node_docker_runtime as docker_runtime`.
- It asserts `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS == docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS`.
- It also asserts `docker_runtime.COMPUTE_ONLY_PATH_ENV_KEYS <= docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS`.

PR-B implication:

- AD-11 must touch `scripts/validate_two_node_docker_runtime.py`.
- Removing only `OBJECT_STORE_ROOT` from `DISPLAY_FORBIDDEN_ENV_KEYS` is not enough because the `COMPUTE_ONLY_PATH_ENV_KEYS <= DISPLAY_FORBIDDEN_ENV_KEYS` assertion would still require it to remain forbidden.
- PR-B should deliberately reclassify `OBJECT_STORE_ROOT` out of compute-only display-forbidden path keys for display runtime, and add it to the display runtime allow/required set if startup requires it.
- Related static documentation/tests generated from these constants may need synchronized updates in PR-B.

## 0.9 OQ7 - Unit Test Injection And SQL Spy Strategy

Status: done.

Files inspected:

- `tests/test_forecast_store_product_quality_sql.py`
- `tests/test_forecast_api.py`
- `tests/test_list_search_contract.py`

Findings:

- `tests/test_forecast_store_product_quality_sql.py` is pure SQL string testing; it does not provide a psycopg connection mock pattern.
- Adjacent tests use small fake cursor/transaction objects when SQL execution needs to be observed, e.g. `SqlCaptureCursor` in `tests/test_forecast_api.py` and `_RecordingCursor` in `tests/test_list_search_contract.py`.

Decision:

- Use Protocol-typed `StationLookup` fakes for parser, path resolution, filtering, response-shape, missing-file, malformed-file, and side-effect tests.
- Add one dedicated spy connection/cursor test for `PsycopgStationLookup` SQL behavior and query counting.

Rationale:

- The reader's core behavior is file parsing and response assembly; forcing all tests through a psycopg mock would couple unrelated parser/filter cases to DB mechanics.
- A focused SQL spy test is still needed for section 1.13s to assert exactly one `met.met_station` lookup and zero `met.forcing_version` / `met.forcing_station_timeseries` queries.

## 0.10 Baseline 200 Response Fixture

Status: done.

Command shape:

```bash
curl -fsS 'https://test.nwm.ac.cn/api/v1/met/stations/heihe_forc_001/series?model_id=basins_heihe_shud&source_id=IFS&cycle_time=2026-06-01T00%3A00%3A00Z'
uv run python - ...  # local sanitizer, replacing only top-level request_id
```

Fixture written:

```text
tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json
```

Sanitization:

- Top-level `request_id` replaced with `"<sanitized-request-id>"`.
- All other structure, field names, ordering, types, and representative values preserved.

Captured response summary:

- HTTP 200
- `status`: `ok`
- station: `heihe_forc_001`
- `model_id`: `basins_heihe_shud`
- `source_id`: `IFS`
- `cycle_time`: `2026-06-01T00:00:00Z`
- series variables: `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, `Press`
- point counts: 53 points per variable

Important gap for section 1.13r:

- The current DB-backed old-cycle baseline includes `Press`.
- AD-5 says the new object-store SHUD CSV reader will not emit `Press` because the CSV has no `Press` column.
- Therefore section 1.13r should compare envelope/station/series item/point field shape, field ordering, and field types for emitted variables. It should not require identical default variable set or identical `Press` presence between the old DB-backed baseline and the new object-store reader.

## Section 1.13r/s/t Context Notes

- Section 1.13r: use the baseline fixture as a schema/field-order oracle, with the `Press` caveat above.
- Section 1.13s: use a dedicated spy cursor/connection around `PsycopgStationLookup`; expected query count is exactly one `met.met_station` lookup and zero forbidden table queries.
- Section 1.13t: node-27 permissions already support the intended side-effect-free model: readable but not writable forcing directories. Unit tests should still assert no write-mode opens, no `mkdir`, and unchanged CSV mtime across repeated reads.
