## Why

The object-store station-series reader is a public display path over files that
can be generated outside the API process. Its safety contract should explicitly
require chunked, bounded line reads so malformed or oversized CSVs cannot force
full-file reads before the reader rejects them.

## What Changes

- Make chunked line reading an explicit object-store station-series contract.
- Add regression coverage for a valid CSV whose logical lines are split across
  multiple small chunks.
- Preserve the existing station-series response shape, variable filtering,
  timestamp handling, row/byte/line caps, and stable error codes.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `object-store-station-series-read`: add explicit chunked-line-read behavior to
  the retained disk CSV reader contract.

## Impact

- `packages/common/object_store_forcing.py`
- `tests/test_object_store_forcing.py`
- `openspec/specs/object-store-station-series-read/spec.md`
