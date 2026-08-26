# basins-asset-discovery Specification

## Purpose
TBD - created by archiving change m9-basins-model-assets. Update Purpose after archive.
## Requirements
### Requirement: Basins root discovery is explicit

The system SHALL discover real SHUD model assets only from an explicit Basins root configured by CLI argument or `NHMS_BASINS_ROOT`, with `data/Basins` allowed as the development default for Basins-specific commands. Unsafe-descendant detection SHALL be errno-driven rather than dependent on any Python version's `pathlib` metadata predicates swallowing an `OSError`: behavior and structured error codes SHALL be identical across supported CPython versions (3.11+).

Root metadata classification SHALL distinguish nonexistence (`ENOENT`/`ENOTDIR`) from unreadability (`EACCES`/`EPERM`). Discovery and migration-report entrypoints SHALL translate both classes into their owning structured error contracts rather than leaking raw `PermissionError`; unreadability SHALL NOT be reported as nonexistence.

#### Scenario: Discover development Basins symlink

- **WHEN** a developer runs the Basins discovery command without an explicit root and `data/Basins` points to `/volume/data/nwm/Basins`
- **THEN** the command scans that root, reports that the source is a development symlink, and records the resolved target path in the inventory

#### Scenario: Missing Basins root does not break fast tests

- **WHEN** the normal fast test command runs in an environment without `/volume/data/nwm/Basins`
- **THEN** Basins real-asset tests are skipped unless explicitly opted in, and synthetic unit tests still validate discovery behavior

#### Scenario: Explicit missing root fails discovery

- **WHEN** the Basins discovery command is run with an explicit `--basins-root` that does not exist
- **THEN** it exits non-zero with `BASINS_ROOT_NOT_FOUND`, includes the root path, and does not produce an importable inventory

#### Scenario: A Basins root denied by an ancestor is unreadable on every interpreter

- **WHEN** discovery or migration-report generation receives a Basins root whose ancestor denies traversal with `EACCES` or `EPERM`
- **THEN** it exits non-zero with the owning structured error carrying `BASINS_ROOT_UNREADABLE` and the root path
- **AND** CPython 3.11 and later interpreters produce the same code
- **AND** no raw `PermissionError` or misleading `BASINS_ROOT_NOT_FOUND` escapes

#### Scenario: Unreadable model directory fails discovery safely

- **WHEN** discovery encounters an unreadable Basins root or unreadable model subdirectory
- **THEN** it exits non-zero with `BASINS_ROOT_UNREADABLE` or `BASINS_DIRECTORY_UNREADABLE`, and does not write an importable inventory

#### Scenario: Symlink escape outside root is rejected

- **WHEN** a candidate model directory is a symlink that resolves outside the configured Basins root
- **THEN** discovery does not follow it as a valid model and reports `BASINS_SYMLINK_OUTSIDE_ROOT` as an error or warning according to the command mode

#### Scenario: Unresolvable symlink descendant blocks importability

- **WHEN** a descendant below the Basins root cannot be strictly resolved because its path contains a symlink loop or another non-permission kernel resolution defect
- **THEN** discovery records the blocking warning `BASINS_SYMLINK_UNRESOLVABLE` for that path and the affected inventory is not importable (`importable` is false, model status is not `valid`, default import is not eligible)
- **AND** this holds identically on every supported CPython version

#### Scenario: Permission denial is not a symlink verdict

- **WHEN** a descendant's strict walk reports `EACCES` or `EPERM`
- **THEN** discovery records a non-symlink unreadability warning rather than `BASINS_SYMLINK_UNRESOLVABLE`
- **AND** it does not silently treat the path as nonexistent
- **AND** containment remains fail-closed when the path cannot be established under the configured root

#### Scenario: Nonexistent descendant is not misclassified as unsafe

- **WHEN** a descendant path merely does not exist (dangling symlink or missing target, kernel `ENOENT` — including paths whose strict walk aborts at a missing component even when the lexical remainder would meet a symlink loop)
- **THEN** discovery does not emit `BASINS_SYMLINK_UNRESOLVABLE` for it and importability of an otherwise-valid inventory is unaffected — nonexistence keeps its silent-skip semantics uniformly across supported CPython versions, is never escalated to an unsafe-path verdict, and never surfaces as an unhandled exception

### Requirement: SHUD model directory inventory is complete

The system SHALL produce a structured JSON inventory for each discovered SHUD model directory containing normalized model identity, source path components, `source_path`, `resolved_source_path`, `source_is_symlink`, `shud_input_name`, `input_dir`, `gis_dir`, required SHUD input files, GIS sidecars, `forcing_dir`, `forcing_dir_original_name`, calibration count, file checksums, known `quirks[]`, validation status, and suggested registry IDs.

The inventory document is hashed raw into the package manifest's `source_inventory_checksum`, which the cutover gate treats as a model identity field. It SHALL therefore carry no field whose value is derived from forcing CSV payloads: the inventory SHALL NOT include a forcing CSV count. Structural forcing facts — which directory was selected and under which spelling — SHALL be retained, because downstream packaging resolves the forcing source directory from them.

#### Scenario: Known 13-model Basins dataset is discovered

- **WHEN** discovery scans the current `data/Basins` dataset
- **THEN** it identifies 13 model directories: `qhh`, `heihe`, `kashigeer`, `weiganhe`, `xinanjiang_upstream`, `hetianhe`, `qinyijiang`, `keliya`, `tailanhe`, and `zhaochen/{WEM,HHY,MC,BST}`

#### Scenario: SHUD input name differs from basin slug

- **WHEN** discovery scans directories such as `kashigeer/input/ksge`, `qinyijiang/input/nanlin`, or `xinanjiang_upstream/input/xinanjiang`
- **THEN** the inventory records both the basin slug from the source path and the `shud_input_name` from `input/<shud_input_name>` without using the input name as the sole model identity

#### Scenario: Legacy forcing directory spelling is normalized

- **WHEN** discovery finds `tailanhe/focing`
- **THEN** the inventory records `forcing_dir` as that path and records a `legacy_focing_dir` quirk

#### Scenario: Forcing directory spelling conflict

- **WHEN** both `forcing/` and `focing/` exist for the same model
- **THEN** discovery either chooses canonical `forcing/` and records a conflict warning, or exits with a structured ambiguity error before producing an importable inventory

#### Scenario: Forcing payload volume does not reach the inventory

- **WHEN** a model has a large forcing directory such as 10000 CSV files, and CSV files are later added to or removed from it
- **THEN** discovery neither counts nor reads the CSV payloads for inventory generation, and the inventory document bytes are unchanged across those payload changes

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

### Requirement: Unreadable required files degrade registration health observably

The discovery checksum walk SHALL treat a required file that was matched but cannot be read, including when strict resolution or later stat/hashing reports `EACCES` or `EPERM`, as a third-state degradation instead of silently skipping or mislabelling it as a symlink defect. The walk SHALL surface the unreadable files as their own collection (mirroring the existing invalid-required-files mechanism — the status expression consumes the collection directly, since quirks alone do not drive status), the model's status SHALL drop from `valid` to `partial` through that collection, an observable quirk marking the model as carrying an unreadable required file SHALL be recorded, and the discovery payload SHALL carry the collection under its existing key alongside the invalid-required-files key.

The missing-required-files semantics stay unchanged (a matched file is never reported as missing), actual unsafe-symlink arms keep their own semantics and are not folded into the permission arm, successful checksum entries keep their existing shape, and a `partial` model remains ineligible for default import and publication.

#### Scenario: A matched but unreadable required file yields partial status

- **WHEN** a required file matched by discovery reports `EACCES` or `EPERM` during strict resolution, stat, or hashing
- **THEN** the model's status is `partial` with an unreadable-required-file quirk
- **AND** the file is named in `unreadable_required_files` and an unreadability warning
- **AND** the file does not appear in `missing_required_files`
- **AND** no `BASINS_SYMLINK_*` warning is used for the permission failure

#### Scenario: Readable required files keep the valid status

- **WHEN** every required file is resolved, read, and hashed successfully
- **THEN** the status and checksum entries are byte-for-byte identical to the pre-change behavior

