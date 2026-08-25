## MODIFIED Requirements

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
