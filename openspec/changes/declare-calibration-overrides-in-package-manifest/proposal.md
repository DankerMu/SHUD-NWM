# Declared calibration overrides, recorded in the package manifest

## Why

#1816 deleted `basins_soil_alpha_repair.py`, which silently rewrote two
calibration parameters during publication. Two of its three findings hold up:
the rewrite was **not recorded anywhere that travels with the package**
(`publish_basins_package` takes no repair argument and the manifest has no field
for one), and it **fired on every basin it scanned**, not only where needed.

The third finding does not hold up. The proposal argued both bounds were
baseless because neither is cited anywhere in the repository, and inferred that
"had the values been fatal, those calibrations could not exist". Measured on
node-22 with hetianhe, an 8-day horizon, and its real warm state, varying only
the two parameters:

| `SOIL_ALPHA` | `GEOL_DMAC` | SHUD range warnings | gfs | IFS |
|---|---|---|---|---|
| 8.193 (source) | **5 (source)** | 37 | **NaN** | — |
| 4.702 | **5** | 10 | **NaN** | — |
| 3.134 (previously published) | **5** | 0 | **NaN** | — |
| 3.134 | 4 (previously published) | 0 | runs | — |
| **8.193 (source)** | **4** | 37 | runs | `rc=0` |
| **8.193 (source)** | **4.5** | 37 | runs | `rc=0` |
| 8.193 (source) | **4.75** | 37 | **NaN** | — |

Every failure is `ERROR: NAN error for QeleSub[i][j] 5` / `EXIT with error code
10(NAN/INF VALUE)`.

So the two bounds are not the same kind of thing:

- **`SOIL_ALPHA <= 20` is real but soft.** `SHUD/src/classes/ModelConfigure.cpp:90`
  declares `checkRange(Alpha, .05, 20., ...)`, but `checkRange`
  (`SHUD/src/Equations/functions.cpp:53`) only prints and returns; `checkValue()`
  discards the result. Alpha runs to completion carrying 37 warnings. The old
  repair cut hetianhe's `SOIL_ALPHA` from 8.193 to 3.134 — a 62% reduction that
  measurement shows was never needed. Deleting this bound was right.
- **`GEOL_DMAC <= 4` is load-bearing and has no SHUD counterpart.** The nearest
  declared range is `checkRange(macD, 0., 10., ...)`
  (`ModelConfigure.cpp:109`); `macD *= g->macD` and the `Dmac` column maxes at
  1.0, so the source value gives `5 x 1.0 = 5`, **inside** `[0, 10]`, emitting
  no warning at all — and still producing NaN. 4.0 is an empirical stability
  bound, which is exactly why no repository citation exists. Deleting it took
  hetianhe out of production.

`SHUD/` is gitignored (`.gitignore:81`), so the #1816 check — a repository grep
— could not have found the first bound, and no grep anywhere could have found
the second.

## What Changes

- **ADDED**: a checked-in, human-maintained calibration-override declaration.
  Each entry names basin, calibration parameter, value, reason, approver and
  date. Nothing is overridden that is not declared.
- **ADDED**: `publish_basins_package` accepts the overrides that applied to a
  package and records them in the manifest under `calibration.overrides`, so the
  record travels with the package instead of living only in a publisher-workspace
  receipt.
- **MODIFIED**: the publisher applies declared overrides on an isolated staging
  copy, never on the Basins source tree, and remains a pure copy for every
  basin and parameter not named in the declaration.
- **ADDED** refusals: a declaration that names an unknown basin, an unknown
  calibration parameter, or an unparseable value fails the publish rather than
  being skipped.
- **ADDED** first entry: `hetianhe` `GEOL_DMAC = 4`. `SOIL_ALPHA` is deliberately
  NOT overridden — the source value 8.193 is preserved.

## Non-goals

- Restoring the scan-everything repair. Overrides apply only where declared.
- Enforcing `SOIL_ALPHA <= 20`. It is a soft SHUD warning and measurement shows
  exceeding it is survivable.
- Choosing calibration values as a platform routine. An override is a recorded
  exception; the durable fix for hetianhe is a recalibration by the modeller.
