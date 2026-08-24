# Design

## D1 — Why deletion, not a warning

An advisory path would keep the bound as a live claim about SHUD while the
bound has no source. The repository cannot say where `20.0` or `4.0` came from,
and the eight drifted basins are evidence *against* them: those calibrations
were produced by running SHUD to convergence at the un-repaired values. A
warning also does not restore the eight basins — they are republished either
way. Keeping a disabled code path would leave the next operator to re-enable
something no one can justify. Delete it.

## D2 — Deletion boundary

Two repairs share the publication plumbing. They are not the same kind of
operation:

| | calibration bounds repair | radiation template repair |
|---|---|---|
| schema | `basins.calibration_repair.v1` | `basins.missing_tsd_rl_template_repair.v1` |
| acts on | a value a human calibrated | a file that is absent |
| recorded in package manifest | no | no |
| recorded in publish receipt `repairs` | yes | yes |
| staging dir | `repaired-basins-soil-alpha` | `repaired-basins` |
| disposition | **deleted** | **kept** |

Only the left column goes. Shared machinery — `PublishContext.repair`, the
summary `repairs` list, `repaired-inventories`, `retain_repair_staging`,
`REPAIR_STAGING_DIR_NAMES[0]`, and
`SCHEDULER_REGISTRY_REPAIRED_MODEL_NOT_PUBLISHABLE` (also raised on the
radiation path at `publish_scheduler_file_registry.py:805`) — stays.

`_merge_repairs` and `basins.scheduler_source_repair.v1` die with the left
column: with one repair kind left there is nothing to merge. `_isolated_root_
for_source_path` had no other caller.

## D3 — What the deletion buys operationally

`_repair_calibrated_shud_context` performed a full `shutil.copytree` of the
basin source — including its forcing tree — in order to edit one number. For
the affected basins that is ~12G copied per publish. The copy dies with the
function.

## D4 — Warm start is not part of this change

The eight-surface `state_compatibility` clone gate
(`packages/common/state_clone.py`, `transfer_mode="recalibration"`) already
excludes `calibration` from the transferability predicate by construction, and
`tests/test_scheduler_lineage.py:359` already proves every `clone_gate_kind`
confers lineage. A clone row is written carrying the *target* package's
version and checksum, so `chain.py:325-336` passes on it without modification.
Nothing in `services/orchestrator/chain.py` changes here. The runtime lineage
check remains the guard that catches a republish where the clone was skipped.

## D5 — Non-goals

- Already-issued forecast history is untouched. 1242 runs / 956 published
  stay as they are; no retraction, no reissue.
- The republish of the eight drifted basins is a separate operation, ordered
  after this change so that republishing cannot re-trigger the repair.
- The `dg_*` identity carriers (#1813) are not touched here.
