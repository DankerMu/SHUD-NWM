## Context

`packages/common/object_store_forcing.py` reads per-station SHUD forcing CSVs
from `OBJECT_STORE_ROOT`. The current reader already uses descriptor-bound
`os.read` and hard limits for total bytes, line bytes, and declared row count.
This change makes that behavior explicit in the OpenSpec fixture and adds a
normal-path regression where a tiny read chunk splits valid logical CSV lines.

## Goals / Non-Goals

**Goals:**

- Preserve descriptor-bound no-follow file access under `OBJECT_STORE_ROOT`.
- Preserve existing byte, line-byte, and row-count caps.
- Prove valid CSV rows can span multiple chunks without changing emitted
  station-series data.
- Keep existing API response and error contracts unchanged.

**Non-Goals:**

- No DB fallback or historical archive read path.
- No frontend cycle-picker changes.
- No `PsycopgForecastStore.station_series()` cleanup.
- No route, OpenAPI, or runtime role behavior changes.

## Decisions

- Keep the reader internal to `packages/common/object_store_forcing.py` and
  expose no new public API. The public contract remains
  `read_station_forcing_csv`.
- Use a test-time tiny `STATION_FORCING_CSV_READ_CHUNK_BYTES` value to force
  multi-read happy-path parsing. This proves chunked behavior without creating
  large fixtures or changing production constants.
- Treat this as an `expanded` fixture because the touched surface is file IO and
  bounded-read behavior on a public API backing helper.

## Risks / Trade-offs

- Regression tests that monkeypatch chunk size could become brittle if the
  internal constant is renamed. Mitigation: keep the test at the module boundary
  and assert observable read calls plus response shape, not private buffer state.
- The reader still materializes bounded parsed tuples for response filtering and
  metadata. That is acceptable because the row cap is explicit; this change is
  about avoiding full-file reads before bounds validation.

## Risk Fixture

Issue type: performance/security hardening
Project profile: NHMS
Blast radius: medium
Fixture level: expanded
Why:
- Shared helper in `packages/common`
- Descriptor-bound file IO under configurable object-store root
- Resource limit / large input behavior

Selected risk packs:
- File IO / path safety / overwrite: selected - station CSV reads must remain
  descriptor-bound and no-follow.
- Resource limits / large input / discovery: selected - chunk, total-byte,
  line-byte, and row-count limits define the safety envelope.
- Error handling / rollback / partial outputs: selected - malformed file errors
  must keep stable `STATION_FORCING_FILE_MALFORMED` mapping.
- Public API / CLI / script entry: selected - helper backs
  `/api/v1/met/stations/{station_id}/series`.
- Schema / columns / units / field names: selected - CSV columns and response
  series contract must not change.
- Legacy compatibility / examples: selected - existing direct-disk behavior and
  tests remain authoritative.
- Documentation / migration notes: selected - OpenSpec records the new
  chunked-read invariant.
- Config / project setup: not selected - no env/config change.
- Auth / permissions / secrets: not selected - no auth/secret surface.
- Concurrency / shared state / ordering: not selected - read-only single-file
  path.
- Release / packaging / dependency compatibility: not selected - no dependency
  or package change.

Domain packs:
- Hydro-met time series / forcing windows: selected - station forcing series
  data and timestamps must remain unchanged.
- Published NHMS artifacts / display identity: selected - object-store artifact
  identity and safe relative paths must remain unchanged.

Invariant Matrix:
- Governing invariant: object-store station forcing CSV reads are
  descriptor-bound, chunked, and bounded before emitting the unchanged
  StationSeriesResponse contract.
- Source-of-truth identity/contract:
  `forcing/{source}/{cycle}/{basin_version_id}/{model_id}/shud/{forcing_filename}`.
- Producers: existing forcing producer outputs; no producer change.
- Validators/preflight:
  `open_file_no_follow`, `_ChunkedBoundedCsvLineReader`, `_parse_csv_header`.
- Storage/cache/query: local object-store file and one allowed
  `met.met_station` lookup.
- Public routes/entrypoints: `read_station_forcing_csv` and station-series API.
- Frontend/downstream consumers: unchanged response shape.
