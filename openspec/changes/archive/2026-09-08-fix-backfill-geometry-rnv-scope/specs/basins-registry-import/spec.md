## ADDED Requirements

### Requirement: Output-river geometry backfill writes only the target network version

`_backfill_output_segment_geometry(cursor, river_network_version_id, *, only_missing)` SHALL match its `UPDATE core.river_segment` on the full composite primary key `(river_segment_id, river_network_version_id)`, binding the function's `river_network_version_id` argument on the write side exactly as both candidate SELECTs already bind it on the read side. `core.river_segment.river_segment_id` carries no single-column uniqueness and its prefix is the `model_id`, not the network version, so the same id text MAY legitimately exist under several `river_network_version_id` values; a backfill for one network SHALL leave every other network's rows byte-identical (`geom`, `length_m`, `properties_json`, and the STORED `stream_type` derived from it), SHALL return a count that includes only the target network's updated rows, and SHALL increment `geometry_generation` only on the target network.

#### Scenario: A same-id output row in a sibling network is untouched

- **WHEN** two `core.river_network_version` rows A and B each hold an output row (`shud_output_river='true'`, `shud_riv_index='1'`, `geom IS NULL`) with the identical `river_segment_id` text, and each holds its own reach source row (`iRiv='1'`, distinct `geom`, `length_m`, and `Type`)
- **AND** `_backfill_output_segment_geometry(cursor, A)` is called
- **THEN** A's output row carries A's reach geometry, `length_m`, `Type`, and `stream_type`
- **AND** B's output row `geom`, `length_m`, `properties_json`, and `stream_type` are unchanged (`geom` and `stream_type` still NULL)

#### Scenario: The returned count and the generation bump describe only the target network

- **WHEN** the backfill above updates exactly one row in A
- **THEN** the call returns 1, not the number of same-id rows across all networks
- **AND** A's `core.river_network_version.geometry_generation` increments by one while B's is unchanged
