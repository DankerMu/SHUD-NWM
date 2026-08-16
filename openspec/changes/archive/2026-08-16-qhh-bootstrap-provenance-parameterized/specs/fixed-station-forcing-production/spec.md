## ADDED Requirements

### Requirement: Bootstrap seed station provenance follows the parameterized project identity

The model-registry bootstrap's station seeding (`_seed_station_rows`) SHALL derive the persisted station provenance labels `properties_json.source` and `properties_json.elevation_metadata.source` from the invocation's `project_name` (as `<project_name>.tsd.forc`), consistent with the row's `project_name` and `forcing_source_identity` fields and with the sibling river-segment lane's parameterized provenance — never from a hardcoded basin literal. Under the default QHH invocation the persisted values SHALL remain byte-identical to `"qhh.tsd.forc"`.

#### Scenario: Non-default project bootstrap writes self-consistent provenance

- **WHEN** the bootstrap seeds station rows with a non-default
  `project_name` (for example `heihe`)
- **THEN** each persisted row's `source` and
  `elevation_metadata.source` equal `heihe.tsd.forc`, its
  `forcing_source_identity` begins with `heihe.tsd.forc:`, its
  `project_name` is `heihe`, and none of these fields carries the
  `qhh` literal

#### Scenario: Default QHH bootstrap output is unchanged

- **WHEN** the bootstrap seeds station rows with the default
  `project_name` `qhh`
- **THEN** the persisted `source` and `elevation_metadata.source`
  values are byte-identical to `"qhh.tsd.forc"`
