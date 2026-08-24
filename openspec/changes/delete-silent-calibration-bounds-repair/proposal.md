# Delete the silent calibration bounds repair

## Why

`workers/model_registry/basins_soil_alpha_repair.py` rewrites externally
calibrated SHUD parameters during package publication, without recording the
rewrite in the package manifest. Measured on node-22 (#1816): **8 of 24
published basins** carry a `cfg.calib` that differs from
`/volume/nwm/Basins/<slug>`, with `SOIL_ALPHA` reduced by 14%–71% and, for
`hetianhe`, `GEOL_DMAC` reduced from 5 to 4. **1242 forecast runs / 956
published** ran on the rewritten values, and for six basins that is their
entire operational history.

The bounds themselves (`SHUD_SOIL_ALPHA_MAX = 20.0`,
`SHUD_GEOL_DMAC_MAX = 4.0`) have **no citation anywhere in the repository** —
not a SHUD source constraint, not a literature value, no ADR, no spec. They
arrived in `243ae565` and `a8322129`, both pushed direct to master with no PR
and no issue. The calibrations they override were produced by external users
running SHUD to convergence; had the values been fatal, those calibrations
could not exist.

Traceability failed alongside the behavior: **zero** of the 24 package
manifests record a repair. The only receipt lives in a scratch directory for
one basin. The other seven rewrites left no trace at all.

## What Changes

- **REMOVED**: `workers/model_registry/basins_soil_alpha_repair.py` in full,
  both parameters (`SOIL_ALPHA` and `GEOL_DMAC`).
- **REMOVED**: its call sites in `scripts/publish_scheduler_file_registry.py` —
  `_repair_calibrated_shud_contexts` / `_repair_calibrated_shud_context`, the
  `SCHEDULER_REGISTRY_CALIBRATION_REPAIR_BLOCKED` refusal, the
  `repaired-basins-soil-alpha` staging `copytree`, and the now-unreachable
  `_isolated_root_for_source_path` / `_merge_repairs` helpers.
- **ADDED** requirement: a published package's calibration files SHALL be
  byte-identical to their source.
- **UNCHANGED**: the radiation-template repair
  (`basins.missing_tsd_rl_template_repair.v1`, `repaired-basins` staging).
  It supplies a *missing* file; it never edits a calibrated value. It keeps
  `retain_repair_staging`, `repaired-inventories`, and the summary
  `repairs` field.
- **UNCHANGED**: already-issued forecast history. The 1242 runs stay as they
  are; this change does not retract or reissue them.

## Impact

- Affected specs: `shud-model-package-publication`
- Affected code: `workers/model_registry/basins_soil_alpha_repair.py`
  (deleted), `scripts/publish_scheduler_file_registry.py`,
  `tests/test_publish_scheduler_file_registry.py`
- Affected docs: `docs/runbooks/current-production-ops.md` §~308
- Downstream: the 8 drifted basins are republished under a separate operation;
  their state carries forward through the existing eight-surface
  `state_compatibility` clone, which excludes `calibration` by construction.
