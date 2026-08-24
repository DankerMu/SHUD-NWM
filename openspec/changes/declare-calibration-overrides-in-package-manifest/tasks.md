# Tasks

## Evidence Floor

- `uv run pytest -q tests/test_basins_package.py tests/test_publish_scheduler_file_registry.py`
- `uv run ruff check .`
- `openspec validate declare-calibration-overrides-in-package-manifest --strict --no-interactive`
- node-22 live: hetianhe republished with the declared override, forcing
  backfilled under the new `model_id`, warm state cloned, failed run released,
  and a bounded pass in which its forecast `succeeded`

## 1. Declaration

- [x] 1.1 A checked-in declaration file: basin, calibration parameter, value,
      reason, approver, date. Nothing is overridden that is not declared.
- [x] 1.2 Refuse an unknown basin, an unknown calibration parameter, an
      unparseable value, and an entry that matched nothing — each with a
      diagnosable error naming the entry.

## 2. Publication

- [x] 2.1 Apply declared overrides on an isolated staging copy, reusing the
      existing staging pattern; assert the Basins source tree is unwritten.
- [x] 2.2 Every basin and parameter not named stays a pure byte copy.
- [x] 2.3 `publish_basins_package` accepts the applied overrides and records
      them under `manifest["calibration"]["overrides"]`.
- [x] 2.4 A package with no overrides carries no override field (absence is
      meaningful; an empty list would be indistinguishable from "not recorded").

## 3. First entry

- [x] 3.1 `hetianhe`: `GEOL_DMAC = 4`, reason recording the measured NaN cliff
      (4.5 runs, 4.75 NaN, source 5 NaN on both gfs and IFS).
- [x] 3.2 `SOIL_ALPHA` is NOT declared for any basin; the source value stands.

## 4. Local verification

- [x] 4.1 `uv run pytest -q tests/test_basins_package.py tests/test_publish_scheduler_file_registry.py`
- [x] 4.2 `uv run ruff check .`
- [x] 4.3 `openspec validate declare-calibration-overrides-in-package-manifest --strict --no-interactive`

## 5. Rollout (node-22, ordered — see design D4)

- [ ] 5.1 Republish hetianhe; record the new `model_id`.
- [ ] 5.2 Backfill forcing under the new id for the cycle the scheduler will run.
- [ ] 5.3 Clone the warm state onto the new id.
- [ ] 5.4 Release the failed run through the manual-retry marker.
- [ ] 5.5 One bounded pass: forecast `succeeded`, state written at the next
      `valid_time`.
- [ ] 5.6 Re-enable `nhms-compute-scheduler.timer`.

## 6. Documentation

- [ ] 6.1 Correct the runbook's provenance note: `SOIL_ALPHA <= 20` is a soft
      SHUD warning (`ModelConfigure.cpp:90`, `checkRange` only prints);
      `GEOL_DMAC <= 4` is an empirical stability bound with no SHUD counterpart.
- [ ] 6.2 Record why a repository grep could not have found either bound
      (`SHUD/` is gitignored; the second exists in no source at all).
