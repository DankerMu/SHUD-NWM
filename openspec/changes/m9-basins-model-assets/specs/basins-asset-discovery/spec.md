## ADDED Requirements

### Requirement: Basins root discovery is explicit

The system SHALL discover real SHUD model assets only from an explicit Basins root configured by CLI argument or `NHMS_BASINS_ROOT`, with `data/Basins` allowed as the development default for Basins-specific commands.

#### Scenario: Discover development Basins symlink

- **WHEN** a developer runs the Basins discovery command without an explicit root and `data/Basins` points to `/volume/data/nwm/Basins`
- **THEN** the command scans that root, reports that the source is a development symlink, and records the resolved target path in the inventory

#### Scenario: Missing Basins root does not break fast tests

- **WHEN** the normal fast test command runs in an environment without `/volume/data/nwm/Basins`
- **THEN** Basins real-asset tests are skipped unless explicitly opted in, and synthetic unit tests still validate discovery behavior

#### Scenario: Explicit missing root fails discovery

- **WHEN** the Basins discovery command is run with an explicit `--basins-root` that does not exist or cannot be read
- **THEN** it exits non-zero, emits a structured error containing the root path and error code, and does not produce an importable inventory

#### Scenario: Unreadable model directory fails discovery safely

- **WHEN** discovery encounters an unreadable Basins root or unreadable model subdirectory
- **THEN** it exits non-zero with `BASINS_ROOT_UNREADABLE` or `BASINS_DIRECTORY_UNREADABLE`, and does not write an importable inventory

#### Scenario: Symlink escape outside root is rejected

- **WHEN** a candidate model directory is a symlink that resolves outside the configured Basins root
- **THEN** discovery does not follow it as a valid model and reports `BASINS_SYMLINK_OUTSIDE_ROOT` as an error or warning according to the command mode

### Requirement: SHUD model directory inventory is complete

The system SHALL produce a structured JSON inventory for each discovered SHUD model directory containing normalized model identity, source path components, `source_path`, `resolved_source_path`, `source_is_symlink`, `shud_input_name`, `input_dir`, `gis_dir`, required SHUD input files, GIS sidecars, `forcing_dir`, `forcing_dir_original_name`, calibration count, forcing count, file checksums, known `quirks[]`, validation status, and suggested registry IDs.

#### Scenario: Known 13-model Basins dataset is discovered

- **WHEN** discovery scans the current `data/Basins` dataset
- **THEN** it identifies 13 model directories: `qhh`, `heihe`, `kashigeer`, `weiganhe`, `xinanjiang_upstream`, `hetianhe`, `qinyijiang`, `keliya`, `tailanhe`, and `zhaochen/{WEM,HHY,MC,BST}`

#### Scenario: SHUD input name differs from basin slug

- **WHEN** discovery scans directories such as `kashigeer/input/ksge`, `qinyijiang/input/nanlin`, or `xinanjiang_upstream/input/xinanjiang`
- **THEN** the inventory records both the basin slug from the source path and the `shud_input_name` from `input/<shud_input_name>` without using the input name as the sole model identity

#### Scenario: Legacy forcing directory spelling is normalized

- **WHEN** discovery finds `tailanhe/focing`
- **THEN** the inventory records `forcing_dir` as that path, includes the forcing CSV count, and records a `legacy_focing_dir` quirk

#### Scenario: Forcing directory spelling conflict

- **WHEN** both `forcing/` and `focing/` exist for the same model
- **THEN** discovery either chooses canonical `forcing/` and records a conflict warning, or exits with a structured ambiguity error before producing an importable inventory

#### Scenario: Large forcing directory is bounded

- **WHEN** a model has a large forcing directory such as 10000 CSV files
- **THEN** discovery records the CSV count using bounded metadata traversal and does not read all CSV payloads for discovery-only inventory generation

### Requirement: Required SHUD files are validated

The system SHALL validate each model `input/<shud_input_name>/` directory for SHUD runtime-required files and report missing or extra-generated files without treating NAS/macOS sidecars as model assets.

#### Scenario: Valid SHUD input package

- **WHEN** an input directory contains `*.cfg.para`, `*.cfg.ic`, `*.cfg.calib`, `*.sp.mesh`, `*.sp.riv`, `*.sp.rivseg`, `*.sp.att`, `*.para.soil`, `*.para.geol`, `*.para.lc`, `*.tsd.forc`, `*.tsd.lai`, `*.tsd.mf`, and `*.tsd.rl`
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
