## Context

The current system already has model registry tables and APIs, SHUD runtime staging, object-store abstractions, and a demo seed. However, most model assets are placeholders such as `s3://nhms/models/yangtze_shud_v12/model_package.tar.gz`.

The development environment now exposes `data/Basins -> /volume/data/nwm/Basins`. A scan confirms 13 SHUD model directories:

- `qhh`, `heihe`, `kashigeer`, `weiganhe`, `xinanjiang_upstream`, `hetianhe`, `qinyijiang`, `keliya`, `tailanhe`
- `zhaochen/WEM`, `zhaochen/HHY`, `zhaochen/MC`, `zhaochen/BST`

Each model generally includes an `input/<shud_input_name>/` directory with SHUD files (`*.cfg.para`, `*.cfg.ic`, `*.cfg.calib`, `*.sp.mesh`, `*.sp.riv`, `*.sp.rivseg`, `*.sp.att`, `*.para.soil`, `*.para.geol`, `*.para.lc`, `*.tsd.forc`, `*.tsd.lai`, `*.tsd.mf`, and usually `*.tsd.rl`), GIS shapefiles under `input/<shud_input_name>/gis/` (`domain`, `river`, `seg`), `CALIB/` candidates, and CMFD forcing CSVs. Several `shud_input_name` values differ from their basin slug, for example `kashigeer/input/ksge`, `qinyijiang/input/nanlin`, and `xinanjiang_upstream/input/xinanjiang`; discovery must preserve both names. `tailanhe` uses `focing/`, which must be normalized as a known legacy spelling, and currently lacks `*.tsd.rl`, so it must be reported as partial unless implementation deliberately supports no-radiation runtime validation.

## Goals / Non-Goals

**Goals:**

- Produce a deterministic Basins inventory and validation report from `data/Basins`.
- Package each valid SHUD model asset into object-store friendly artifacts with checksums and a manifest.
- Register basin, basin version, river network, mesh, and model metadata using existing model registry contracts.
- Provide at least one real-asset smoke path through SHUD runtime staging and API/river-segment listing.
- Document production migration so the real data is copied, not the development symlink.

**Non-Goals:**

- Do not run the real `shud_omp` solver or validate hydrological numerical skill in this stage.
- Do not ingest all long historical forcing CSVs into `met`/`hydro` tables.
- Do not implement CLDAS, real MinIO/S3, or real Slurm cluster validation.
- Do not redesign the core database schema unless an existing table cannot represent required metadata.
- Do not make fast unit tests require `/volume/data/nwm/Basins`.

## Decisions

### 1. Treat `data/Basins` as an opt-in real-asset source

Use `NHMS_BASINS_ROOT` or a CLI `--basins-root` argument, defaulting to `data/Basins` only for explicit Basins workflows. Fast tests must skip or use synthetic fixtures when the directory is absent.

Alternatives considered:

- Make `data/Basins` mandatory for all tests: rejected because the path is environment-specific and a symlink in development.
- Copy Basins into the repo: rejected because real model assets are large production data, not source code.

### 2. Discovery outputs an inventory before writing registry rows

The first artifact should be a JSON inventory containing discovered model ID, basin ID, path components, `source_path`, `resolved_source_path`, `source_is_symlink`, `shud_input_name`, `input_dir`, `gis_dir`, `forcing_dir`, `forcing_dir_original_name`, required-file status, counts, checksums, `quirks[]`, package fields, and suggested registry IDs. Registry writes consume this inventory rather than crawling ad hoc each time.

Rationale: dry-run review, reproducibility, issue evidence, and production migration planning all need a stable manifest.

### 3. Package SHUD inputs separately from historical forcing payloads

The model package should include SHUD runtime-required `input/<shud_input_name>/` files, selected `CALIB/` metadata, GIS sidecars, and package manifest. Long historical forcing CSVs should be represented in the package manifest with count/checksum metadata and optionally copied under a separate object-store prefix.

Rationale: runtime staging needs compact model inputs; historical forcing can be large and should not be duplicated unintentionally.

For #135, package publication is immutable and does not implement a force-overwrite path. If source checksums differ for an existing `<model_id>/<version>` manifest, the command must fail with `BASINS_PACKAGE_CHECKSUM_CONFLICT`; operators must publish a new version instead. Object-store keys are deterministic:

- runtime package files: `models/<model_id>/<version>/package/<relative_path>`
- package manifest: `models/<model_id>/<version>/manifest.json`
- explicit forcing copy: `models/<model_id>/<version>/forcing/<relative_path>`

### 4. Preserve existing registry contracts

Use the current tables and APIs:

- `core.basin` / `core.basin_version`
- `core.river_network_version` / `core.river_segment`
- `core.mesh_version`
- `core.model_instance`

Add parser/import code if needed, but avoid schema churn unless existing metadata cannot store source URI/checksum/properties.

### 5. Parse GIS and SHUD files conservatively

For production import, `input_dir/gis/domain.shp` should provide basin geometry, `input_dir/gis/river.shp` or `input_dir/gis/seg.shp` should provide line geometries, and SHUD `.sp.riv`/`.sp.rivseg` should provide counts and topology metadata. If shapefile parsing dependencies are unavailable, implementation may start with validation and package manifests, but registry import tasks must include a real geometry parse path before completion.

### 6. Model IDs must be deterministic and collision-safe

Suggested model IDs should use normalized source path components rather than only the input directory name, for example `basins_qhh_shud`, `basins_kashigeer_shud`, `basins_qinyijiang_shud`, and `basins_zhaochen_wem_shud`, with corresponding version IDs such as `<basin>_vbasins_YYYYMMDD`, `<basin>_rivnet_vbasins_YYYYMMDD`, and `<basin>_mesh_vbasins_YYYYMMDD`. Original path casing and `shud_input_name` must remain in metadata. The import must be idempotent and must not overwrite an active model without an explicit activation action.

### 7. Use the current activation contract and persisted audit evidence

Activation must use the current API/implementation contract `PUT /api/v1/models/{model_id}/active` with body `{ "active": true }` or compatible `active_flag`. This stage does not require a new audit table; acceptable audit evidence is an existing persisted event if available, otherwise structured logs plus API/DB evidence that only the requested model became active.

For #137, the repository already has `ops.audit_log`, so the activation path should write a durable audit row for every successful model active-state transition. The audit row should identify the `model_instance`, requested active flag, previous active flag, actor/role defaults, and enough model lineage (`basin_version_id`, `river_network_version_id`, `mesh_version_id`, `model_package_uri`) to prove the explicit activation path was used. Repeating an already-active/already-inactive request remains a conflict and should not create a new audit row.

## Risks / Trade-offs

- Basins directories contain inconsistent names and NAS sidecar files such as `@eaDir` and `.DS_Store` -> filter known generated files and record quirks in inventory.
- Some directories are partial, including `tailanhe` missing `*.tsd.rl` -> distinguish valid, partial, and invalid assets in inventory; only valid or explicitly accepted partial assets can be published/imported.
- `tailanhe/focing` typo can break uniform discovery -> normalize it as `forcing_dir` while preserving original path in metadata.
- Large forcing CSVs can make package jobs slow -> separate runtime input package from historical forcing inventory/copy.
- Shapefile parsing can introduce new dependencies -> choose a small, documented parser dependency or use an existing GDAL/pyogrio stack if already available; tests should cover missing-sidecar errors.
- Development symlink can hide production migration failures -> migration command must reject symlink-only targets when producing production evidence.

## Migration Plan

1. Add Basins discovery and validation in dry-run mode with synthetic tests plus optional `data/Basins` smoke.
2. Add deterministic package manifest and local object-store publication for one small model, then all discovered valid models.
3. Add registry import from inventory/package manifest with idempotent DB behavior.
4. Add API/runtime/frontend-adjacent smoke checks.
5. Update `progress.md`, validation docs, and production migration notes with evidence and copy-not-symlink requirements.
6. Rollback is removal of imported registry rows and object-store package prefixes for this stage; source Basins data remains external.

## Open Questions

- Which Basins model should become the first default active staging model: smallest runtime package (`WEM`/`MC`) or a domain-priority basin such as `qhh`?
- Should all historical forcing CSVs be copied during first production migration, or should only runtime input and calibration packages be copied initially with forcing archived separately?

## Workflow Fixture

Fixture level: expanded

Project profile: other, with SHUD model-package and geospatial data surfaces.

Change surface:

- `workers/model_registry/cli.py` and new model-registry discovery/package/import modules.
- `packages/common/model_registry.py` and `apps/api/routes/models.py` when registry/API behavior is consumed.
- Object-store package publication and local file traversal under `data/Basins`.
- PostGIS/shapefile parsing paths for registry import.
- Frontend generated API types and model asset fixtures when API fields change.

Must preserve:

- Fast `uv run pytest -q` must not require `/volume/data/nwm/Basins`, real object storage, real Slurm, or a real SHUD solver.
- Existing demo seed and existing model registration APIs remain compatible.
- Existing `nhms-model validate-package`, `validate_model_package_path`, and `validate_model_package_uri` behavior remains compatible.
- Imported Basins models remain inactive unless explicitly activated.
- Development symlink `data/Basins` must not be treated as valid production migration evidence.

Must add/change:

- Explicit Basins discovery inventory, package publication, migration report, registry import, and runtime/API/frontend consumption workflows.
- Structured error behavior for missing roots, partial assets, checksum conflicts, sidecar failures, and transaction rollback.

Selected risk packs:

- Public API / CLI / script entry: new `nhms-model` subcommands and existing model activation API.
- Config / project setup: `NHMS_BASINS_ROOT`, `OBJECT_STORE_ROOT`, `OBJECT_STORE_PREFIX`, and opt-in real-asset tests.
- File IO / path safety / overwrite: symlink handling, root traversal, package writes, idempotency, checksum conflicts.
- Schema / columns / units / field names: inventory JSON, package manifest, registry payloads, OpenAPI/frontend fields.
- Geospatial / CRS / shapefile sidecars: `domain`, `river`, and `seg` shapefile sidecars plus PostGIS geometry.
- Time series / forcing / temporal boundaries: forcing CSV metadata, header coverage, and no-bulk-copy default.
- Resource limits / large input / discovery: thousands of forcing CSV files and bounded inventory/package behavior.
- Legacy compatibility / examples: `tailanhe/focing`, NAS/macOS sidecars, input directory aliases such as `ksge` and `nanlin`.
- Error handling / rollback / partial outputs: missing root, partial assets, geometry/count mismatch, registry transaction rollback.
- Release / packaging / dependency compatibility: optional shapefile parser dependency and Linux CI/dev setup.
- Documentation / migration notes: production copy-not-symlink evidence and validation docs.

Risk packs considered:

- Public API / CLI / script entry: selected - new `nhms-model` subcommands; evidence in tasks 1.1, 2.2, 2.7, 3.1, 4.6.
- Config / project setup: selected - `NHMS_BASINS_ROOT` and opt-in real-asset smoke; evidence in tasks 1.1 and 1.7.
- File IO / path safety / overwrite: selected - symlink roots, sidecar filtering, package writes; evidence in tasks 1.3-1.7 and 2.6-2.7.
- Schema / columns / units / field names: selected - inventory JSON and package manifest fields; evidence in tasks 1.3 and 2.1.
- Geospatial / CRS / shapefile sidecars: selected - GIS sidecars and PostGIS import; evidence in tasks 3.2-3.4.
- Time series / forcing / temporal boundaries: selected - forcing CSV metadata and header coverage; evidence in tasks 1.5 and 2.4-2.5.
- Numerical stability / conservation / NaN: not selected - real SHUD numerical execution is a non-goal for this stage.
- Solver runtime / performance / threading: not selected - runtime coverage is limited to dry-run/mock staging.
- Resource limits / large input / discovery: selected - thousands of forcing CSV files and bounded traversal; evidence in tasks 1.3, 1.5, 1.7, 2.4.
- Legacy compatibility / examples: selected - `tailanhe/focing`, input aliases, sidecars, existing validator behavior; evidence in tasks 1.4-1.7 and preservation text.
- Error handling / rollback / partial outputs: selected - structured discovery errors, partial assets, conflicts, registry rollback; evidence in tasks 1.3, 1.6, 2.6, 3.7.
- Release / packaging / dependency compatibility: selected - optional parser dependencies and Linux CI/dev setup; evidence in tasks 3.3, 5.3-5.5.
- Documentation / migration notes: selected - copy-not-symlink and source quirks; evidence in tasks 5.1-5.2.

Required evidence:

- Discovery synthetic tests: missing root exits non-zero with stable error code and no importable inventory; CLI arg takes precedence over `NHMS_BASINS_ROOT`; symlink root records `source_is_symlink` and `resolved_source_path`; unreadable directories return structured errors; bounded traversal ignores recursive sidecar directories and does not follow paths outside the root.
- Discovery inventory tests: sidecar filtering, exact JSON fields, input alias preservation, `tailanhe/focing`, `forcing`/`focing` conflict, partial `*.tsd.rl`, stable model IDs, forcing CSV counts.
- Gated real Basins smoke: current 13-model inventory when `data/Basins` is available.
- Package tests: manifest checksum, idempotency, checksum conflict, forcing metadata/default no-copy, explicit forcing copy.
- Migration report tests: symlink target fails, real copied target passes.
- Registry integration tests: domain geometry import, segment count/topology, rollback on mismatch, inactive default.
- Runtime/API/frontend tests: dry/mock staging, model listing/activation, river segment pagination, OpenAPI/generated types when fields change.

Non-goals:

- Real `shud_omp` execution, hydrological skill validation, CLDAS implementation, real Slurm cluster, real MinIO/S3, and full historical forcing ingestion are out of scope.

Review focus:

- Path and symlink safety; fixture must not make external Basins mandatory for fast tests.
- Deterministic inventory/package contracts and checksum/idempotency behavior.
- Geometry sidecar validation and no partial registry writes.
- Existing model APIs and frontend consumers remain compatible.

### #136 Registry Import Design Notes

`nhms-model import-basins-registry` must treat the Basins discovery inventory and package manifest as its only source-of-truth inputs. It may open source files referenced by those manifests for geometry and SHUD evidence, but it must not rediscover arbitrary directories under `data/Basins`.

The import should run as one database transaction per selected model so GIS sidecar failures, parser failures, checksum conflicts, and segment-count mismatches leave no partial `core.*` rows. Existing rows for the same deterministic IDs are reusable only when their checksum/source metadata still matches the incoming manifest and inventory; changed checksums require a new version ID.

Geometry parsing should be isolated from database writes. The parser layer must validate required shapefile sidecars before reading `domain`, `river`, or `seg`, convert basin geometry to a non-empty MultiPolygon compatible with SRID 4490, convert river features to LineString geometry, and reconcile feature counts against `.sp.riv`/`.sp.rivseg` evidence before persistence.

The model import must keep Basins models inactive by default. Source lineage belongs in existing checksum/source fields where available and in `resource_profile`/properties JSON for fields that lack dedicated columns, including `manifest_uri`, `package_checksum`, `source_inventory_checksum`, `basin_slug`, `shud_input_name`, and source path metadata.
