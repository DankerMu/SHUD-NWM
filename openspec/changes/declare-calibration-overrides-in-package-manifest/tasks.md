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
- [x] 1.2 For a basin this run PUBLISHES, refuse an unknown calibration
      parameter, an unparseable value, and an entry that matched nothing —
      each with a diagnosable error naming the entry.  A declared basin the run
      does not publish is reported on the summary, not refused (the refusal key
      is *published but not applied*).
- [x] 1.3 Both lanes load the checked-in declaration by default — the manual
      publisher CLI and `scheduler_file_provider_refresh`.  The flag / env var
      only redirects the path.

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

Identity changes on a direct-grid topology go through the **direct merged
publish** of runbook 5.7.1, NOT a cutover declaration. The runbook is explicit:
the refresh-side gate cannot see a direct-grid identity change, so a declaration
matches nothing and, once stale, makes EVERY later refresh refuse with
`registry_cutover_declaration_invalid` — stalling the daily pipeline. This is
also why an unwired refresh lane refuses with `registry_cutover_undeclared`
rather than reverting anything: the gate is closed in both directions.

- [ ] 5.1 Republish hetianhe with the declared override; record the new
      `model_id` and `package_checksum`.
- [ ] 5.2 Merge-publish the registry directly (`publish_scheduler_registry_manifest`,
      one shared `generated_at` for both targets, `expected_preimage` CAS on the
      canonical side). Assert before publishing: row count unchanged, no
      duplicate `model_id`, all rows `direct_grid`, per-basin row count unchanged.
- [ ] 5.3 Backfill forcing under the new id for the cycle the scheduler will run
      (`scripts/node22_backfill_forcing_for_model_ids.py`).
- [ ] 5.4 Clone the warm state onto the new id.
- [ ] 5.5 Release the failed run through the manual-retry marker
      (`scripts/node22_manual_retry_failed_runs.py`).
- [ ] 5.6 One bounded pass: forecast `succeeded`, state written at the next
      `valid_time`.
- [ ] 5.7 Re-enable `nhms-compute-scheduler.timer`.

## 6. Documentation

- [x] 6.1 Correct the runbook's provenance note: `SOIL_ALPHA <= 20` is a soft
      SHUD warning (`ModelConfigure.cpp:90`, `checkRange` only prints);
      `GEOL_DMAC <= 4` is an empirical stability bound with no SHUD counterpart.
- [x] 6.2 Record why a repository grep could not have found either bound
      (`SHUD/` is gitignored; the second exists in no source at all).
