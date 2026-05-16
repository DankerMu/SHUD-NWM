## 1. Basins Discovery and Validation

- [x] 1.1 Add a Basins discovery module and CLI entry, accepting `--basins-root` and `NHMS_BASINS_ROOT`, with `data/Basins` as the explicit development default.
- [x] 1.2 Implement recursive model directory discovery for direct basin folders and nested `zhaochen/*` folders, producing deterministic normalized model IDs and registry ID suggestions.
- [x] 1.3 Define and test the Basins inventory JSON schema, including `basin_slug`, `source_path`, `resolved_source_path`, `source_is_symlink`, `shud_input_name`, `input_dir`, `gis_dir`, `forcing_dir`, validation status, quirks, checksums, and suggested registry IDs.
- [x] 1.4 Validate required SHUD runtime input files under `input/<shud_input_name>/`, excluding `.DS_Store`, `@eaDir`, and `*@SynoEAStream` sidecars.
- [x] 1.5 Normalize forcing directory discovery for both `forcing/` and legacy `focing/`, recording conflicts, quirks, original directory name, and CSV counts in inventory.
- [x] 1.6 Mark partial assets such as `tailanhe` missing `*.tsd.rl` as partial/invalid unless an explicit acceptance option is used.
- [x] 1.7 Add synthetic unit tests for missing root, CLI/env precedence, symlink root fields, unreadable root, sidecar recursion, input aliases, `tailanhe/focing`, `forcing`/`focing` conflict, missing `*.tsd.rl`, stable JSON fields, and stable error codes.
- [x] 1.8 Add an opt-in real `data/Basins` smoke test that verifies the current 13-model inventory when the symlink is available, while default fast tests skip this dependency.
- [x] 1.9 Preserve existing `nhms-model validate-package`, `validate_model_package_path`, and `validate_model_package_uri` behavior with regression coverage.
- [x] 1.10 Add explicit discovery test fixtures:
  - missing root: CLI `discover-basins --basins-root /missing` -> non-zero, `BASINS_ROOT_NOT_FOUND`, no inventory file.
  - CLI/env precedence: env root A plus CLI root B -> inventory `root` equals B.
  - symlink root: root symlink to copied fixture -> `source_is_symlink=true`, `resolved_source_path` set.
  - valid model tree: minimal `basin/input/alias` files plus `CALIB/` and `forcing/` -> `status=valid`, expected fields present.
  - partial model tree: missing `*.tsd.rl` -> `status=partial` or `invalid`, missing file listed.
  - sidecar recursion: `.DS_Store`, `@eaDir/*`, `*@SynoEAStream` -> excluded from counts/checksums.
  - forcing conflict: both `forcing/` and `focing/` -> canonical choice with warning or structured ambiguity error.
  - bounded large input: many forcing CSV files -> count computed without loading all file contents into memory.

### #134 Discovery Fixture Matrix

All #134 fixtures use the command shape `nhms-model discover-basins --basins-root <root> --output <tmp>/inventory.json` unless the case explicitly tests env/default behavior. Successful commands exit `0` and write inventory JSON. Failing commands exit non-zero, print JSON or a stable text error containing `error_code`, and do not write an importable inventory.

Fixture A - missing root:

```text
<tmp>/missing-root/              # path does not exist
```

- Invocation: `nhms-model discover-basins --basins-root <tmp>/missing-root --output <tmp>/inventory.json`
- Expected: exit non-zero; `error_code=BASINS_ROOT_NOT_FOUND`; `<tmp>/inventory.json` absent.

Fixture B - CLI/env precedence:

```text
<tmp>/root-a/a/input/a/...       # valid minimal tree
<tmp>/root-b/b/input/alias/...   # valid minimal tree
```

- Invocation: `NHMS_BASINS_ROOT=<tmp>/root-a nhms-model discover-basins --basins-root <tmp>/root-b --output <tmp>/inventory.json`
- Expected: exit `0`; inventory `root` equals `<tmp>/root-b`; discovered model uses `basin_slug=b`, `shud_input_name=alias`; no records from `root-a`.

Fixture C - symlink root:

```text
<tmp>/real-basins/qhh/input/qhh/...
<tmp>/linked-basins -> <tmp>/real-basins
```

- Invocation: `nhms-model discover-basins --basins-root <tmp>/linked-basins --output <tmp>/inventory.json`
- Expected: exit `0`; inventory root entry has `source_is_symlink=true`, `resolved_source_path=<tmp>/real-basins`; model record keeps `source_path` under `<tmp>/linked-basins`.

Fixture D - valid minimal model tree:

```text
<root>/basin-a/input/alias-a/alias-a.cfg.para
<root>/basin-a/input/alias-a/alias-a.cfg.ic
<root>/basin-a/input/alias-a/alias-a.cfg.calib
<root>/basin-a/input/alias-a/alias-a.sp.mesh
<root>/basin-a/input/alias-a/alias-a.sp.riv
<root>/basin-a/input/alias-a/alias-a.sp.rivseg
<root>/basin-a/input/alias-a/alias-a.sp.att
<root>/basin-a/input/alias-a/alias-a.para.soil
<root>/basin-a/input/alias-a/alias-a.para.geol
<root>/basin-a/input/alias-a/alias-a.para.lc
<root>/basin-a/input/alias-a/alias-a.tsd.forc
<root>/basin-a/input/alias-a/alias-a.tsd.lai
<root>/basin-a/input/alias-a/alias-a.tsd.mf
<root>/basin-a/input/alias-a/alias-a.tsd.rl
<root>/basin-a/input/alias-a/gis/domain.{shp,shx,dbf,prj}
<root>/basin-a/input/alias-a/gis/river.{shp,shx,dbf,prj}
<root>/basin-a/input/alias-a/gis/seg.{shp,shx,dbf,prj}
<root>/basin-a/CALIB/top01.calib
<root>/basin-a/forcing/X1.csv
```

- Expected: exit `0`; model record has `status=valid`, `basin_slug=basin-a`, `shud_input_name=alias-a`, `model_id=basins_basin_a_shud`, `forcing_dir_original_name=forcing`, `calibration_count=1`, `forcing_csv_count=1`, required file roles present, and generated sidecar count `0`.

Fixture E - partial model missing radiation:

```text
<root>/tailanhe/input/tlh/...    # same as valid fixture but without tlh.tsd.rl
<root>/tailanhe/focing/X1.csv
```

- Expected: exit `0`; model record has `basin_slug=tailanhe`, `shud_input_name=tlh`, `status=partial` or `invalid`, `missing_required_files` contains role or glob for `*.tsd.rl`, `quirks` contains `legacy_focing_dir`, and default publish/import eligibility is false.

Fixture F - sidecar recursion:

```text
<root>/qhh/input/qhh/.DS_Store
<root>/qhh/input/qhh/@eaDir/qhh.cfg.para@SynoEAStream
<root>/qhh/input/qhh/gis/@eaDir/domain.shp@SynoEAStream
<root>/qhh/forcing/@eaDir/X1.csv@SynoEAStream
```

- Expected: exit `0`; sidecar files are absent from required role matches, `forcing_csv_count`, `calibration_count`, and checksum inputs; `quirks` or warnings include generated sidecars ignored.

Fixture G - forcing spelling conflict:

```text
<root>/conflict/input/conflict/...  # valid input
<root>/conflict/forcing/X1.csv
<root>/conflict/focing/X2.csv
```

- Expected: either exit non-zero with `error_code=BASINS_FORCING_DIR_CONFLICT`, or exit `0` with `forcing_dir_original_name=forcing` and a conflict warning. The implementation must choose one behavior and test it explicitly.

Fixture H - symlink escape / out-of-root traversal:

```text
<tmp>/outside/escape/input/escape/...  # valid tree outside root
<root>/escape-link -> <tmp>/outside/escape
```

- Expected: discovery does not follow `escape-link` as a model outside the Basins root; either model is absent with warning `BASINS_SYMLINK_OUTSIDE_ROOT`, or command fails with that error code and no importable inventory.

Fixture I - bounded large forcing directory:

```text
<root>/large/input/large/...      # valid input
<root>/large/forcing/X000001.csv ... X010000.csv
```

- Expected: exit `0`; `forcing_csv_count=10000`; implementation uses bounded metadata collection and does not read all CSV payloads to compute discovery inventory unless a later package step explicitly requests aggregate payload checksums.

Fixture J - unreadable root or model directory:

```text
<tmp>/unreadable-root/            # chmod 000 when supported
<root>/locked-model/              # chmod 000 model subdirectory when supported
```

- Invocation: `nhms-model discover-basins --basins-root <tmp>/unreadable-root --output <tmp>/inventory.json`, or a readable root containing `locked-model`.
- Expected: exit non-zero with `error_code=BASINS_ROOT_UNREADABLE` for unreadable root or `BASINS_DIRECTORY_UNREADABLE` for unreadable model subdirectory; no importable inventory is written. Tests may skip chmod-specific assertions on platforms/filesystems that cannot enforce permissions.

## 2. Package Publication and Migration Evidence

- [x] 2.1 Define the Basins package manifest schema with source path, normalized IDs, required files, GIS sidecars, calibration metadata, forcing metadata, per-file checksums, and package checksum.
- [x] 2.2 Add explicit `nhms-model publish-basins` command contract and tests for local object-store publication.
- [x] 2.3 Implement object-store publication for runtime SHUD input packages and selected calibration/GIS metadata, returning stable `model_package_uri` values.
- [x] 2.4 Keep bulk historical forcing CSVs out of the runtime package by default while recording forcing count, sample header coverage, and aggregate checksum metadata.
- [x] 2.5 Add explicit historical forcing copy option that writes forcing payloads to a separate object-store prefix and records URI/checksum evidence.
- [x] 2.6 Make publication idempotent when source file checksums and target version are unchanged, and reject checksum changes for the same version unless an explicit new `--version` is used.
- [x] 2.7 Implement `nhms-model basins-migration-report` so production evidence fails for symlink targets and passes for real copied directories with file count, byte count, and inventory checksum.

### #135 Package / Migration Fixture Matrix

All #135 package fixtures consume a Basins discovery inventory generated by `nhms-model discover-basins` or an equivalent synthetic inventory fixture. Fast tests must use synthetic Basins trees and a local `OBJECT_STORE_ROOT`; real `data/Basins` package smoke is opt-in.

Fixture K - valid package publication:

```text
<root>/basin-a/input/alias-a/...       # valid SHUD input + gis sidecars
<root>/basin-a/CALIB/top01.calib
<root>/basin-a/forcing/X000001.csv
<object-root>/
```

- Invocation: `OBJECT_STORE_ROOT=<object-root> OBJECT_STORE_PREFIX=s3://nhms nhms-model publish-basins --inventory <inventory.json> --model-id basins_basin_a_shud --version vbasins-test --output <manifest.json>`
- Expected: exit `0`; writes manifest JSON; output includes `status=published`, stable `model_package_uri`, `manifest_uri`, `package_checksum`, per-file checksums, and package file entries for runtime input, GIS sidecars, selected `CALIB/`, and manifest.
- Object-store layout: runtime package files live under `models/<model_id>/<version>/package/`, manifest lives at `models/<model_id>/<version>/manifest.json`, and explicit forcing copy lives under `models/<model_id>/<version>/forcing/`.
- Manifest minimum fields: `schema_version`, `model_id`, `version`, `model_package_uri`, `manifest_uri`, `package_checksum`, `source_inventory_checksum`, `source_path`, `resolved_source_path`, `source_is_symlink`, `included_files[]`, `forcing`, `calibration`, `created_at`.
- File entry shape: `{"relative_path": "...", "object_uri": "...", "size_bytes": 123, "sha256": "...", "role": "runtime_input|gis|calibration|manifest|forcing"}`.
- Manifest file entries use `relative_path=manifest.json` and `role=manifest`. To avoid a recursive checksum fixed-point, `package_checksum` covers source/package/forcing material and excludes the manifest self-entry; the manifest self-entry checksum covers the deterministic manifest payload before that self-entry is appended, while `size_bytes` records the final object-store manifest byte length.
- CLI success payload shape: `{"status": "published|already_done", "model_id": "...", "version": "...", "model_package_uri": "...", "manifest_uri": "...", "package_checksum": "..."}`.

Fixture L - publication idempotency:

- Invocation: run Fixture K twice with unchanged source and target version.
- Expected: second run exits `0` with `status=already_done` or equivalent; `model_package_uri`, `manifest_uri`, and `package_checksum` remain unchanged.

Fixture M - checksum conflict for same version:

- Invocation: run Fixture K, mutate a required source file, then rerun with the same model/version without `--force` or new version.
- Expected: non-zero exit or structured failure payload with `error_code=BASINS_PACKAGE_CHECKSUM_CONFLICT`; existing manifest/package is not silently overwritten.
- Explicit overwrite is a non-goal for #135: there is no `--force` overwrite path in this issue. Users must choose a new `--version` when source checksums change.

Fixture N - no bulk forcing copy by default:

```text
<root>/basin-a/forcing/X000001.csv ... X000010.csv
```

- Expected: default package publication records forcing directory metadata, CSV count, sample header/time coverage when parsable, and aggregate checksum evidence, but package file entries do not include `forcing/*.csv` payloads.

Fixture O - explicit forcing copy:

- Invocation: Fixture K plus `--copy-forcing`.
- Expected: forcing CSV payloads are copied under a separate object-store prefix; manifest records `forcing_payload_uri`, forcing file count, byte count, and aggregate checksum.

Fixture P - partial inventory rejected:

- Invocation: publish a model with `status=partial` such as missing `*.tsd.rl`.
- Expected: non-zero exit with stable `error_code=BASINS_MODEL_NOT_PUBLISHABLE` unless a later issue explicitly defines a partial acceptance mode.

Fixture P2 - structured failure payload:

- Expected: all `publish-basins` and `basins-migration-report` command failures print JSON to stderr with at least `error_code`, `message`, and whichever of `model_id`, `version`, `path`, or `manifest_uri` is relevant. Commands must not claim `status=published` after a failure; checksum-conflict failures must preserve the previous manifest/package.

Fixture Q - production migration symlink rejection:

```text
<tmp>/real-basins/...                 # real copied fixture
<tmp>/linked-basins -> <tmp>/real-basins
```

- Invocation: `nhms-model basins-migration-report --basins-root <tmp>/linked-basins --output <report.json>`.
- Expected: non-zero exit with `error_code=BASINS_MIGRATION_SYMLINK_TARGET`; report states production must copy actual data and must not rely on symlink-only evidence.

Fixture R - production migration copied target:

- Invocation: `nhms-model basins-migration-report --basins-root <tmp>/real-basins --source-uri /volume/data/nwm/Basins --output <report.json>`.
- Expected: exit `0`; report records `source_is_symlink=false`, file count, byte count, inventory checksum, source-to-target metadata, and `production_ready=true`.

## 3. Registry Import

- [ ] 3.1 Add explicit `nhms-model import-basins-registry` command contract that consumes inventory/package manifests rather than crawling source directories ad hoc.
- [ ] 3.2 Implement import from Basins inventory/package manifests into `core.basin` and `core.basin_version`, including domain geometry from `input_dir/gis/domain.shp` and sidecar validation.
- [ ] 3.3 Implement river network parsing in a focused parser layer for `input_dir/gis/{river,seg}` and SHUD `.sp.riv`/`.sp.rivseg` evidence.
- [ ] 3.4 Implement river network import into `core.river_network_version` and `core.river_segment`, reconciling segment counts and persisting topology metadata where available.
- [ ] 3.5 Implement mesh/model import into `core.mesh_version` and `core.model_instance`, setting `model_package_uri`, checksum/source metadata, resource profile defaults, and inactive-by-default active flags.
- [ ] 3.6 Ensure repeated imports are idempotent, reject checksum conflicts for unchanged version IDs, and do not alter existing active models unless an explicit activation path is used.
- [ ] 3.7 Add real PostgreSQL/PostGIS integration coverage for one small Basins fixture or gated real Basins import path, including transaction rollback on geometry/count mismatch.

## 4. Runtime, API, and Frontend Consumption

- [ ] 4.1 Add a SHUD runtime staging smoke that uses a Basins-backed `model_package_uri` in dry-run or mock mode and verifies staged control/input files.
- [ ] 4.2 Add API smoke tests showing imported Basins models appear in model listing/active discovery after explicit activation.
- [ ] 4.3 Add river-segment API smoke showing imported Basins river features return paginated GeoJSON-compatible records for map rendering.
- [ ] 4.4 Implement or update model asset detail API/OpenAPI contract so Basins-backed fields include basin/model names, segment count, mesh ID, calibration ID, package URI/checksum, active flag, and source lineage.
- [ ] 4.5 Regenerate frontend API types and add frontend/store-level fixture coverage so the model asset management page can consume Basins-backed model metadata without placeholder-only data.
- [ ] 4.6 Verify explicit activation through `PUT /api/v1/models/{model_id}/active`, including audit event or equivalent structured log plus API/DB proof.

## 5. Documentation and Validation

- [ ] 5.1 Update `progress.md` and validation docs with Basins discovery, packaging, registry import, and production migration commands.
- [ ] 5.2 Document known source quirks, including `tailanhe/focing` and NAS/macOS sidecar filtering.
- [ ] 5.3 Run and record OpenSpec strict validation and `uv run ruff check .`.
- [ ] 5.4 Run and record backend unit tests for discovery/package/import plus the gated real-asset smoke when `data/Basins` is available.
- [ ] 5.5 Run and record relevant API/OpenAPI/frontend checks, including generated type freshness when OpenAPI changes.
