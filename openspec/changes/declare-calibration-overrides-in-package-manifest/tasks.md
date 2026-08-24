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
- [x] 1.4 A declared basin absent from the DISCOVERED INVENTORY refuses with
      `CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY` naming the entry — a typo or
      a stale slug is a broken deploy, and silently reverting a basin to its
      NaNing source value is strictly worse than a loud non-committing failure.
      A basin that IS in the inventory but is filtered out of this run keeps
      report-not-refuse, under the distinct token
      `basin_not_selected_for_this_run`.
- [x] 1.5 The unattended refresh lane's receipt names the failure: reason
      `calibration_override_invalid` plus a `calibration_overrides` block
      carrying the raw `CALIBRATION_OVERRIDE_*` code and the offending entry.
      It also records the not-applied entries, which that lane previously never
      persisted at all.
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

## 5. Rollout (node-22 + node-27, ordered — see design D4)

Identity changes on a direct-grid topology go through the **direct merged
publish** of runbook 5.7.1, NOT a cutover declaration. The runbook is explicit:
the refresh-side gate cannot see a direct-grid identity change, so a declaration
matches nothing and, once stale, makes EVERY later refresh refuse with
`registry_cutover_declaration_invalid` — stalling the daily pipeline. This is
also why an unwired refresh lane refuses with `registry_cutover_undeclared`
rather than reverting anything: the gate is closed in both directions.

**The merged manifest publish goes LAST — after the clone rows exist.** Runbook
5.7.1 calls the order a hard constraint but understates why (its text predates
#1164): publishing first does not merely block one cycle fail-safe, it ADMITS
the run as `PACKAGED_IC_BOOTSTRAP` from the packaged IC
(`services/orchestrator/scheduler_generation.py:1057-1078`), publishing a
forecast that starts from the package instead of the carried-over warm state —
a discontinuous production hydrograph. See D4.

**Precondition, checked before 5.1**: `nhms-compute-scheduler.timer` is disabled
and `nhms-compute-scheduler.service` is not active for the whole of 5.1-5.7.
This closes the admission window structurally rather than relying on the
ordering alone. (It is already disabled — it was stopped when hetianhe started
NaNing.)

- [ ] 5.0 Provision the new model instance on node-27 (`core.model_instance`),
      per runbook 5.7.1's first step.
- [ ] 5.1 Republish hetianhe with the declared override; record the new
      `model_id` and `package_checksum`. Assert the packaged `*.cfg.calib`
      carries `GEOL_DMAC 4` and differs from the Basins source file in exactly
      that one line.
- [ ] 5.2 Backfill forcing under the new id for the cycle the scheduler will run
      (`scripts/node22_backfill_forcing_for_model_ids.py`, lands with #1825 /
      PR #1833). Depends on 5.1's `model_id`, not on the manifest publish.
- [ ] 5.3 Clone the warm state onto the new id — both state-index copies (NFS
      and scratch), `valid_time` equal to the target cycle `C` itself, not
      `C − lead` (runbook 5.7.1).
- [ ] 5.4 **Now** merge-publish the registry directly
      (`publish_scheduler_registry_manifest`, one shared `generated_at` for both
      targets, `expected_preimage` CAS on the canonical side). Assert before
      publishing: row count unchanged, no duplicate `model_id`, all rows
      `direct_grid`, per-basin row count unchanged.
- [ ] 5.5 Manual refresh to rebuild readiness; judge by `latest.json`'s
      `started_at` going newer, NOT by receipt file count (`latest.json` is
      overwritten in place).
- [ ] 5.6 Release the failed run through the manual-retry marker
      (`scripts/node22_manual_retry_failed_runs.py`, #1825).
- [ ] 5.7 One bounded pass: forecast `succeeded`, state written at the next
      `valid_time`, and the transition decision is `warm_continue` — NOT
      `PACKAGED_IC_BOOTSTRAP`. That assertion is the whole point of the ordering
      above.
- [ ] 5.8 Mark the old rows `superseded` only AFTER the new run is published —
      `superseded` is not in the display candidate whitelist
      (`packages/common/forecast_store.py`), so marking early blanks the basin's
      frontend.
- [ ] 5.9 Re-enable `nhms-compute-scheduler.timer`.

## 6. Documentation

- [x] 6.1 Correct the runbook's provenance note: `SOIL_ALPHA <= 20` is a soft
      SHUD warning (`ModelConfigure.cpp:90`, `checkRange` only prints);
      `GEOL_DMAC <= 4` is an empirical stability bound with no SHUD counterpart.
- [x] 6.2 Record why a repository grep could not have found either bound
      (`SHUD/` is gitignored; the second exists in no source at all).
