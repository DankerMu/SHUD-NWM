# basins-asset-discovery (delta)

## ADDED Requirements

### Requirement: Registration Rejects Malformed IC Headers Fail-Closed

Baseline registration SHALL validate the content shape of every matched
`*.cfg.ic` header before the model becomes registrable: the header line MUST
carry exactly three or four numeric tokens (the native
`<mesh> <mesh-state-columns> <minute-time>` layout or the compatibility
`<mesh> <river> <lake> <minute-time>` layout), and the leading mesh count
MUST match the element count declared on the first line of the model's
single matched `.sp.mesh` file — a model with more than one matched
`.sp.mesh` is rejected as ambiguous with its own distinct reason. A model whose IC header fails either check SHALL be
rejected from registration fail-closed with a locatable reason naming the
offending file path and the actual numeric-token count, without aborting
discovery of the remaining models. When multiple `*.cfg.ic` files match,
every matched file is validated and any malformed one rejects the model. A
matched IC file whose header line cannot be read SHALL also block
registration fail-closed, with its own distinct reason — unreadability
SHALL NOT be reported as a shape violation, and a shape violation SHALL NOT
be reported as unreadability.

#### Scenario: A two-token IC header is rejected at registration

- **GIVEN** a basin model directory whose `*.cfg.ic` header line is
  `23106\t6` (two numeric tokens, no minute-time)
- **WHEN** basins discovery inventories the model
- **THEN** the model is rejected from registration with a reason that names
  the IC file path and the numeric-token count `2`, and discovery of other
  models continues

#### Scenario: A mesh-count mismatch is rejected at registration

- **GIVEN** a model whose IC header is a well-formed three-token layout but
  whose leading mesh count differs from the `.sp.mesh` first-line element
  count
- **WHEN** basins discovery inventories the model
- **THEN** the model is rejected fail-closed with a reason naming both
  counts

#### Scenario: Well-formed layouts keep registering

- **GIVEN** a model whose IC header carries three or four numeric tokens
  with a mesh count matching `.sp.mesh`
- **WHEN** basins discovery inventories the model
- **THEN** registration proceeds exactly as before this change

#### Scenario: An unreadable matched IC blocks registration distinctly

- **GIVEN** a model whose `*.cfg.ic` is matched by glob but whose header
  line cannot be read (for example an unreadable file mode or an I/O error)
- **WHEN** basins discovery inventories the model
- **THEN** the model is rejected fail-closed with an unreadability reason
  distinct from the shape-violation reason

## MODIFIED Requirements

### Requirement: Required SHUD files are validated

The system SHALL validate each model `input/<shud_input_name>/` directory for SHUD runtime-required files and report missing or extra-generated files without treating NAS/macOS sidecars as model assets. A package whose required files are all present but whose `*.cfg.ic` header fails the content-shape validation (or cannot be read) is NOT valid.

#### Scenario: Valid SHUD input package

- **WHEN** an input directory contains `*.cfg.para`, `*.cfg.ic`, `*.cfg.calib`, `*.sp.mesh`, `*.sp.riv`, `*.sp.rivseg`, `*.sp.att`, `*.para.soil`, `*.para.geol`, `*.para.lc`, `*.tsd.forc`, `*.tsd.lai`, `*.tsd.mf`, and `*.tsd.rl`, and every matched `*.cfg.ic` passes the header content-shape validation
- **THEN** validation marks the SHUD input package as valid and lists the matched files

#### Scenario: Partial SHUD input package

- **WHEN** an input directory lacks a normally required file such as `*.tsd.rl`
- **THEN** validation marks the model as `partial` or `invalid`, records the missing file, and prevents default publication/import unless an explicit acceptance flag is provided

#### Scenario: Generated sidecars are ignored

- **WHEN** discovery encounters `.DS_Store`, `@eaDir`, or `*@SynoEAStream` files
- **THEN** those files are excluded from required-file matching and package checksums, while a warning is recorded for source hygiene

#### Scenario: Generated sidecar directories are ignored recursively

- **WHEN** an `@eaDir/` directory contains mirrored shapefile or SHUD input sidecar files
- **THEN** discovery does not count those files as model, GIS, forcing, or checksum evidence
