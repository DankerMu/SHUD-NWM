# Spec Delta: fixed-station-forcing-production（station-provenance-actual-member）

## ADDED Requirements

### Requirement: Station handoff provenance records the actually-resolved forcing-index member

The DB-free file plane's station-inventory handoff (`_handoff_station_rows`) SHALL derive each station row's `properties_json.source` default from the package manifest's `files` list — selecting entries whose role is the SHUD forcing-index role AND whose `relative_path` is an accepted station-index member, preferring the canonical member when multiple match — and SHALL write that member's basename. When no such member resolves, the handoff SHALL omit the `source` key rather than fabricate a value. A station property that already carries `source` SHALL be preserved unchanged.

#### Scenario: Canonical package labels stations with the canonical basename

- **WHEN** the handoff assembles station rows for a package whose manifest lists the canonical station-index member (`shud/stations.tsd.forc`)
- **THEN** station rows without a pre-existing `source` property carry `"source": "stations.tsd.forc"`, for every basin identically (no QHH-specific literal)

#### Scenario: Legacy replay package labels stations with the legacy basename truthfully

- **WHEN** the handoff runs against a legacy package whose manifest lists only `shud/qhh.tsd.forc` as the station-index member
- **THEN** station rows carry `"source": "qhh.tsd.forc"` — now naming a member that actually exists in that package

#### Scenario: Unresolvable member yields absent provenance, not a fabricated label

- **WHEN** the package manifest lacks a resolvable station-index member (missing `files`, wrong shape, or no role/membership match)
- **THEN** emitted station rows contain no `source` key in `properties_json`, and the handoff otherwise proceeds unchanged

#### Scenario: Pre-existing provenance is never overwritten

- **WHEN** a station's properties already include a `source` value (e.g. QHH bootstrap real-asset stations)
- **THEN** the handoff preserves that value verbatim
