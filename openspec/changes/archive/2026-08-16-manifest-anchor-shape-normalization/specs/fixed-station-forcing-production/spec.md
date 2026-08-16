## MODIFIED Requirements

### Requirement: Forcing package station-index identity is basin-neutral and fails closed

The SHUD forcing package main station-index member SHALL carry the fixed basin-neutral canonical identity `shud/stations.tsd.forc`, with the legacy identity `shud/qhh.tsd.forc` accepted read-only for historical packages. Producers SHALL emit only the canonical member. Direct-grid consumers SHALL resolve the station index by requiring exactly one member from the {canonical, legacy} set in both the package manifest and the staged filesystem, failing closed on ambiguity or absence and on a manifest-declared member that is absent from the object tree. Non-direct-grid staging, which copies the whole package prefix and can therefore legitimately hold a residual second member from an in-place re-produce, SHALL resolve a multi-member filesystem by the declared member from the package manifest, or the run manifest's diagnostic file list when the package manifest publishes none, with canonical-first fallback instead of failing. No consumer SHALL treat the member filename as evidence of the forcing data's basin identity. The declaration-source matching SHALL accept the same entry shapes the direct-grid consumer accepts for the identical manifest: a `./`-prefixed `relative_path` normalizes before the accepted-member intersection, and an entry that omits `relative_path` resolves through its `uri` relative to the forcing package root; an entry that is invalid or underivable under these rules is skipped, because the anchor is a best-effort resolver and SHALL NOT introduce a new fail-closed surface on the non-direct-grid lane.

#### Scenario: canonical member published for every basin

- **WHEN** a direct-grid forcing package is produced for any basin, QHH included
- **THEN** the main station-index member is `shud/stations.tsd.forc` with manifest role `shud_forcing`
- **AND** the station rows and per-station CSVs derive from that basin's own inputs, not from the member filename.

#### Scenario: legacy package remains consumable

- **WHEN** a historical package whose only station-index member is `shud/qhh.tsd.forc` is staged
- **THEN** the runtime resolves, checksums, and stages it exactly as before the migration
- **AND** existing object-store packages are not rewritten.

#### Scenario: ambiguous index membership fails closed

- **WHEN** a direct-grid package manifest lists more than one station-index member from the {canonical, legacy} set, or a direct-grid package's staged filesystem contains both files
- **THEN** the runtime raises the fail-closed error `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS` naming the conflicting members
- **AND** no fallback member selection is attempted.

#### Scenario: non-direct-grid staging resolves a residual second member by manifest

- **WHEN** a non-direct-grid package's staged filesystem contains both station-index files, as a full-prefix copy can after a pre-migration prefix is re-produced in place
- **THEN** the runtime resolves the member to stage from the first available declaration source — the checksum-verified package manifest's `files` list when it publishes a non-empty one, otherwise the run manifest's diagnostic `forcing.files` entries — staging the member that source names when it names exactly one accepted member, and the canonical member otherwise
- **AND** the run does not fail on the residual member.

#### Scenario: missing index membership fails closed for direct-grid

- **WHEN** a direct-grid package contains no station-index member from the {canonical, legacy} set
- **THEN** the existing missing-member errors are raised and their messages name both accepted identities.

#### Scenario: manifest-declared member absent from the object tree fails closed with an accurate code

- **WHEN** a direct-grid package manifest declares one accepted station-index identity but the object tree carries only the other
- **THEN** the runtime fails closed with a checksum-read error naming the declared member, not a size-limit error.

#### Scenario: real QHH model asset keeps its identity

- **WHEN** the real QHH model asset `data/Basins/qhh/input/qhh/qhh.tsd.forc` or its bootstrap tooling is exercised
- **THEN** its QHH-specific naming and semantics are unchanged by this contract.

#### Scenario: dot-prefixed manifest declaration still anchors the legacy member

- **WHEN** a non-direct-grid staged filesystem carries both station-index members and the declaration source names the legacy member with a `./`-prefixed `relative_path` (a shape the direct-grid checksum lane already accepts)
- **THEN** the anchor resolves the legacy member and the run does not silently fall back to the stale canonical member

#### Scenario: uri-only manifest declaration still anchors the legacy member

- **WHEN** the declaration source's entry for the legacy member omits `relative_path` and carries only a `uri` located under the forcing package root
- **THEN** the anchor derives the member from the `uri` and resolves the legacy member

#### Scenario: an underivable entry is skipped without failing the lane

- **WHEN** a declaration-source entry is invalid or its `uri` is not under the forcing package root
- **THEN** that entry is skipped, the anchor falls back to canonical-first when no other entry names exactly one accepted member, and no error is raised on the non-direct-grid lane

#### Scenario: a declaration naming both accepted members still falls back

- **WHEN** the declaration source names both accepted station-index members, in plain, dot-prefixed, or uri-only shapes
- **THEN** the anchor returns no member and staging falls back to canonical-first, preserving the pre-existing ambiguity semantics
