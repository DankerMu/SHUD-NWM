## ADDED Requirements

### Requirement: Basins package publication validates river-segment reach mappings before identity or output

Before producing a Basins package source identity or creating any environment-backed object-store root, package child, lock, object, manifest, or local output receipt, the system SHALL validate the canonical `.sp.riv` and `.sp.rivseg` files selected by existing required-file validation. Both public package entrypoints SHALL use one shared validator and SHALL return the same stable mapping error semantics.

Each mapping file SHALL be opened as a verified regular file beneath the model source root without following symlink components. The byte-size check and bounded read SHALL apply to that same open descriptor; each file SHALL be rejected above a fixed mapping-byte limit, read by at most that limit plus one detection byte, and decoded as strict UTF-8. File content, raw exceptions, and unbounded parser diagnostics SHALL NOT enter an error payload.

The reader SHALL skip a bounded number of blank/comment lines beginning with `#`, `//`, or `%`; the first remaining line SHALL declare a positive row count in its first token. Within a declared block, blank/comment lines SHALL NOT count as data rows. After the count line, at most one exact standard non-numeric header row SHALL be accepted. For `.sp.riv`, exactly the next declared reach rows SHALL form the reach table and later topology/coordinate blocks SHALL NOT contribute reach identities. Every reach row SHALL carry a unique bounded integer `Index`; reach identities MAY be non-contiguous and non-1-based. For `.sp.rivseg`, exactly the declared segment rows SHALL carry bounded integer `Index` and `iRiv` columns, no additional non-comment data row SHALL follow that declared block, and every `iRiv` SHALL belong to the actual reach `Index` set. Integer tokens SHALL be ASCII decimal with an optional sign and a fixed digit bound before conversion.

Malformed, unreadable, invalid-UTF-8, over-limit, count/header/column-invalid, truncated, duplicate-reach, or missing-reference inputs SHALL fail with `BASINS_RIVSEG_MAPPING_INVALID` and bounded cause details. When `reach_count > 1` and `segment_count > 1` but all segment rows reference one reach, validation SHALL fail with `BASINS_RIVSEG_MAPPING_DEGENERATE` and bounded reach, segment, and mapped-reach counts plus the mapped reach ID. A genuine one-reach package SHALL remain valid; validation SHALL NOT require every declared reach to receive a segment.

The mapping bytes used by source identity and publication SHALL be the same bounded snapshots that passed validation. A path replacement after validation SHALL NOT cause different invalid mapping bytes to enter identity or immutable storage. For valid inputs, validation SHALL otherwise be observational: package/source-identity schemas, checksum material, object keys, manifests, forcing behavior, calibration bytes/declarations, ordinary non-mapping source replacement behavior, and downstream registry/runtime behavior remain unchanged.

#### Scenario: Valid multi-reach mapping reaches both public package seams

- **WHEN** a canonical package declares multiple actual reach Index values and multiple segment rows whose `iRiv` values cover more than one of those reaches
- **THEN** `basins_package_source_identity` and package publication both succeed
- **AND** their pre-existing identity, manifest, forcing, and immutable-write behavior remains compatible.

#### Scenario: Actual non-contiguous reach indices are authoritative

- **WHEN** the declared reach block has unique Index values `{10, 30}` and every segment `iRiv` belongs to `{10, 30}`
- **THEN** validation succeeds without requiring the inferred range `1..reach_count`.

#### Scenario: Comments, a standard header, and trailing river blocks preserve declared rows

- **WHEN** either mapping has bounded blank/comment lines, a positive count, an exact optional header, and the declared data rows, while `.sp.riv` also carries standard trailing topology/coordinate blocks
- **THEN** comments/headers are not counted as data, only the declared reach block supplies reach Index values, and a valid mapping succeeds.

#### Scenario: A segment references no actual reach

- **WHEN** any segment `iRiv` is absent from the actual unique `.sp.riv.Index` set
- **THEN** source identity and publication fail with `BASINS_RIVSEG_MAPPING_INVALID` carrying a bounded sorted missing-ID set
- **AND** no environment-backed store root, package child, lock, object, manifest, or local output receipt is created.

#### Scenario: A multi-reach mapping collapses to one reach

- **WHEN** both declared counts exceed one but all segment rows reference one actual reach
- **THEN** both public seams fail with `BASINS_RIVSEG_MAPPING_DEGENERATE`
- **AND** the bounded details record reach count, segment count, mapped-reach count, and mapped reach ID
- **AND** no output-side effect occurs.

#### Scenario: A genuine single-reach package remains compatible

- **WHEN** a package declares one reach and otherwise-valid segment rows reference that reach
- **THEN** validation succeeds and the package retains its existing identity and publication behavior.

#### Scenario: Malformed or over-limit mapping bytes fail bounded

- **WHEN** either mapping is invalid UTF-8, exceeds or grows beyond the byte cap, carries an overlong integer, has an invalid/non-positive count, invalid header, duplicate reach Index, missing column, truncated declared block, or an additional segment data row
- **THEN** both public seams return `BASINS_RIVSEG_MAPPING_INVALID` without leaking a raw decode/parser exception or unbounded file content
- **AND** no output-side effect occurs.

#### Scenario: Validated bytes remain bound through final publication

- **WHEN** a valid `.sp.rivseg` is replaced with different invalid bytes after mapping validation but before the final package write
- **THEN** the replacement bytes do not enter source identity or the package object
- **AND** any published mapping object and manifest checksum refer to the validated snapshot bytes.

#### Scenario: Calibration and sibling package consumers remain unchanged

- **WHEN** an otherwise valid package includes forcing and checked-in calibration declarations and is consumed by registry/QHH/object-store validation paths
- **THEN** validation changes no source bytes, identity/checksum shape, forcing policy, or calibration override
- **AND** the exact seven-entry checked-in calibration declaration remains unchanged
- **AND** existing downstream consumers keep accepting the valid package.
