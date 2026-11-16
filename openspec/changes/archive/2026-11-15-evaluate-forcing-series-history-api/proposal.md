## Why

The direct object-store station-series route correctly returns
`STATION_FORCING_FILE_NOT_FOUND` for cycles outside node-27's retained disk
window. Historical station forcing data may still be useful, but silently
falling back to DB rows would blur freshness, retention, and provenance
semantics in the hot display path.

## What Changes

- Record the #631 evaluation outcome: long-term history is a valid future API
  direction, but it must be explicit and separate from the current disk route.
- Define freshness, retention, provenance, and error-code semantics for that
  future surface.
- Add an ADR so future implementation work has a stable decision record.

## Capabilities

### New Capabilities

- None in runtime behavior.

### Modified Capabilities

- `met-station-series-api`: clarify future historical/archive access semantics.

## Impact

- `docs/adr/0001-station-forcing-history-api-boundary.md`
- `docs/runbooks/object-store-forcing-series-read.md`
- `openspec/specs/met-station-series-api/spec.md`
