# direct-grid-binding-artifact (delta)

## MODIFIED Requirements

### Requirement: Station identity and filenames are safe and immutable

The mapping builder SHALL embed the immutable mapping-asset identity in each `station_id` and produce safe, pathless `forcing_filename`s that never collide with reserved names, including both the canonical and legacy station-index identities.

#### Scenario: station_id embeds immutable mapping-asset identity

- **WHEN** the builder assigns a `station_id`
- **THEN** the `station_id` embeds the immutable mapping-asset identity and is never reused across mapping versions
- **THEN** the identity is chosen so the database mirror fails closed on collision rather than reusing an id across versions.

#### Scenario: forcing_filename is safe, pathless, and collision-free

- **WHEN** the builder assigns a `forcing_filename`
- **THEN** the filename is safe, pathless, and case-fold unique across the binding
- **THEN** the filename does not collide with the canonical station-index name `stations.tsd.forc`, the legacy station-index name `qhh.tsd.forc`, the manifest, debug artifacts, or model-input filenames, including on case-insensitive filesystems
- **THEN** the filename is not derived from rounded coordinates.
