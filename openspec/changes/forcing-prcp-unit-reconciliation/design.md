# Design — Forcing PRCP unit reconciliation

## Evidence (as found)

| Layer | PRCP unit / behavior | Source |
|-------|----------------------|--------|
| SHUD model contract | `mm/day` | `SHUD/VersionUpdate.md:25`; `AutoSHUD/Rfunction/LDAS_UnitConvert.R:11,35,84-86`; `AutoSHUD/SubScript/Step5x_Analysis.R:57` (`'Prcp (mm/day)'`) |
| Producer output label | `mm` (doc: "per-timestep accumulated mm") | `workers/forcing_producer/producer.py:52`, docstring `:1193-1201` |
| GFS canonical | `mm` (per-step) → factor `1.0` | `converter.py:51`; `producer.py:1212` |
| ERA5 canonical | `mm/day` → factor `step/24` (per-step) | `converter.py:62,709`; `producer.py:1207-1209` |
| IFS canonical | `mm` per-step → factor `24/step` (daily) | `converter.py:67,809`; `producer.py:1211` |

Net: GFS and ERA5 emit per-timestep mm; IFS emits a daily total. The producer labels all three `mm`. The SHUD contract says `mm/day`. The three positions are mutually inconsistent; per-source PRCP can be off by `24/step` (8× at 3h).

## Open question to resolve first (blocking the fix direction)

What unit does **this repo's** SHUD runtime actually read from `qhh.tsd.forc` PRCP, given `DT_QE_PRCP 1440` (1440 min = 1 day) and `TS_PRCP 1`? Two consistent end-states are possible:

- **A. Target = `mm/day` (matches SHUD doc).** Then IFS (`24/step`) is correct; GFS (`1.0`) and ERA5 (`step/24`, i.e. issue #256's Agent A change) are wrong and must become `24/step` and `1.0` respectively; `OUTPUT_UNITS["PRCP"]` becomes `mm/day`.
- **B. Target = per-timestep `mm` (matches current GFS/ERA5 + label).** Then GFS and ERA5 are correct, IFS (`24/step`) is the lone bug and must become `1.0`; the SHUD doc/label discrepancy is documented or the SHUD timestep config is shown to consume per-step amounts.

Do not pick a branch by reading the code's current majority; verify against the SHUD consumer (model run on a known forcing input, or rSHUD/AutoSHUD ingestion contract) before changing factors. The merged GFS path (#254/#255) being "green" only proves internal consistency, not physical correctness against SHUD.

## Resolution

**Verified SHUD PRCP unit = `mm/day`. Decision A selected.**

The authoritative unit that the SHUD runtime reads from `qhh.tsd.forc` PRCP is `mm/day`, confirmed against the AutoSHUD/rSHUD ingestion contract that this pipeline must match:

| Evidence | What it shows |
|----------|---------------|
| `AutoSHUD/Rfunction/LDAS_UnitConvert.R` | Every LDAS adapter emits the precip column named `Precip_mm.d` with the inline comment "to mm/day (SHUD)" — the ingestion contract normalizes every source to a daily rate before SHUD reads it. |
| `SHUD/VersionUpdate.md:25` | Documents the SHUD forcing PRCP unit as `mm/day`. |
| `AutoSHUD/SubScript/Step5x_Analysis.R:57` | Labels the analyzed precip series `'Prcp (mm/day)'`, i.e. the consumed series is a daily rate. |
| `DT_QE_PRCP 1440` | `DT_QE_PRCP` = 1440 min = 1 day: the precip forcing is integrated over a 1-day window, consistent with a `mm/day` rate, not a per-step accumulation. |

Because the consumed unit is `mm/day`, the IFS branch (`24/step`) was already correct, while GFS (`1.0`, per-step mm) and ERA5 (issue #256's `step/24`, per-step mm) were wrong. All three branches and `OUTPUT_UNITS["PRCP"]` are now reconciled to `mm/day`:

- `mm/day` canonical (ERA5) -> factor `1.0` (passthrough, step-independent).
- per-step `mm` canonical (GFS, IFS) -> factor `24 / step_hours` (GFS requires a resolvable step; IFS falls back to the configured `ifs_precip_step_hours`).
- any other unit -> `ForcingProductionError` ("no documented PRCP->mm/day conversion").

## Data migration note

This change alters the numeric PRCP magnitude for two already-merged source paths (no silent value change — recorded here):

- **GFS**: previously emitted per-step `mm` (factor `1.0`); now multiplied by `24/step` (e.g. ×8 at a 3h step). Existing GFS forcing snapshots and any hydro results derived from them may need regeneration.
- **ERA5**: previously converted `mm/day -> step/24` per-step `mm`; now passes through unchanged (`×1.0`). Existing ERA5 forcing snapshots and downstream hydro results may need regeneration.
- **IFS**: unchanged (`24/step` was already correct); no migration required.

The migration is **executable, not just advisory**: `producer_version` is bumped `m1.0 -> m2.0` and is now part of the `forcing_version` currency check (`_existing_forcing_version_is_current` compares both the stored lineage and the on-disk manifest lineage). Any `forcing_version` produced before this change (lineage `producer_version` `m1.0`, or a manifest lineage missing the field) is therefore judged non-current on the next `produce()` for the same cycle and is force-recomputed with the corrected mm/day conversion — old per-step bytes can no longer be short-circuited to `already_done` and relabelled.

## Decision criteria

1. Confirm the consumed unit empirically (SHUD run with a unit-probe forcing, or authoritative rSHUD/AutoSHUD ingestion code path that this pipeline must match).
2. Make all three branches + `OUTPUT_UNITS["PRCP"]` agree with that unit.
3. Pin per-source numeric magnitude in tests so the convention cannot silently drift again.

## Risk / compatibility

- Whichever direction is chosen changes the numeric PRCP magnitude for at least one already-merged source path, so existing forcing snapshots / hydro results produced before this change may need regeneration. Record this as a data-migration note, not a silent value change.
- This is a `MODIFIED` capability on `fixed-station-forcing-production`; identity binding, station contract, and packaging are unchanged.
