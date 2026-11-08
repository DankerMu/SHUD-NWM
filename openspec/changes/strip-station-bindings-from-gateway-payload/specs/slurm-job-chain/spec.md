## ADDED Requirements

### Requirement: Gateway-bound task payloads SHALL NOT carry direct-grid station binding tables

The orchestrator SHALL remove `station_bindings` and `stations` from `resource_profile.direct_grid_forcing` in every manifest entry it sends to the Slurm gateway and in every manifest index file it writes. This covers the array `tasks`, the stage manifest's `manifest_index`, and the publish stage's `basins`. All other `resource_profile` and `direct_grid_forcing` keys SHALL be kept unchanged. The projection SHALL NOT mutate the orchestrator's in-memory basins, which SHALL keep the full station binding table. Entries whose `resource_profile` or `direct_grid_forcing` is absent or not a mapping SHALL pass through unchanged apart from the `state_evidence` removal.

#### Scenario: An array submission carries no station binding table
- **WHEN** the orchestrator submits an array stage for direct-grid basins whose `resource_profile.direct_grid_forcing` carries `station_bindings`
- **THEN** no `tasks[*]` entry and no `manifest.manifest_index[*]` entry sent to the gateway contains `station_bindings` or `stations` under `resource_profile.direct_grid_forcing`
- **AND** the written `<stage>_manifest_index.json` contains neither
- **AND** each entry keeps `direct_grid_forcing.binding_uri`, `direct_grid_forcing.binding_checksum` and `resource_profile.package_checksum`
- **AND** the orchestrator's active basins still carry their `station_bindings`

#### Scenario: Entries without a direct-grid block pass through
- **WHEN** an entry has no `resource_profile`, a non-mapping `resource_profile`, no `direct_grid_forcing`, a non-mapping `direct_grid_forcing`, or a `direct_grid_forcing` mapping with neither `station_bindings` nor `stations`
- **THEN** its gateway entry equals the input entry without `state_evidence`
- **AND** its `resource_profile` value is the same object as the input entry's
