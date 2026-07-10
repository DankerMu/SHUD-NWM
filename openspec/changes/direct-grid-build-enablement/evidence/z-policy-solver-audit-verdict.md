# z_policy Solver-Audit Verdict

**Change:** `direct-grid-build-enablement` (Capability `z-policy-verdict`)
**Audit scope:** narrow — three questions only (grill decision D10). This is the
constrained revival of the solver forcing-consumer audit that was intentionally
descoped from the platform-readiness change (`cmfd-direct-grid-platform-readiness`,
Epic #886, proposal.md: "Solver forcing-consumer auditing is intentionally out of
scope: the migration does not touch the solver, and the production `shud` binary is
treated as stable"). It does **not** re-audit the full solver; it answers only the
questions that gate the `z_policy` verdict per docs §7.5 and §P0.3.

**Audit target (pinned):** SHUD solver submodule commit
`3aec65755926c478e13ca7d4fea80715e4e90345`.

**Provenance / how this verdict was obtained (local oracle):** the SHUD solver
source is vendored in-tree at `SHUD/` and its checked-out `HEAD` is exactly the
pinned commit, so the audit was performed directly against the pinned source. Verify:

```bash
git -C SHUD rev-parse HEAD
# => 3aec65755926c478e13ca7d4fea80715e4e90345   (matches the pin)
```

Node-22 re-verification (equivalent oracle, per CLAUDE.md routing for solver source /
production `.cfg`):

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'git -C /scratch/frd_muziyao/NWM/SHUD rev-parse HEAD'
# expect: 3aec65755926c478e13ca7d4fea80715e4e90345
```

All line references below are against this pinned tree.

---

## Verdict

```
verdict = model_dem_at_cell_center
```

`model_dem_at_cell_center` is an **explicit elevation source** (§7.5). It is the
authoritative `z_policy` input for the mapping builder's binding stage
(`workers/mapping_builder/binding.py`) for the direct-grid pilot.

`sentinel` is **rejected** for the general case (see Q1–Q3). `canonical_orography`
is the scientifically-ideal end state but is **not available today** (the Change 2
registry does not store met-product orography) and is deferred as the preferred
future upgrade.

---

## Q1 — Does SHUD's `.tsd.forc` read chain read the station-row `Z` column?

**Answer: YES.**

- The per-station forcing time series struct stores the parsed station row's
  `X`/`Y`/`Z`: `SHUD/src/classes/TimeSeriesData.hpp:31`

  ```cpp
  double  xyz[3]={NA_VALUE, NA_VALUE, NA_VALUE};
  ```

- The forcing-provider abstraction exposes the station elevation as a first-class
  accessor: `SHUD/src/classes/ForcingProvider.hpp:33,63`

  ```cpp
  virtual double z(int station_idx) const = 0;               // :33 (metadata: "z in meters")
  double z(int station_idx) const override {                 // :63
      return tsd_weather_[station_idx].xyz[2];
  }
  ```

The station `Z` column is therefore parsed and retained; it is not discarded at read
time.

## Q2 — What is the station `Z` used for?

**Answer: exactly one numerical consumer — temperature elevation (lapse-rate)
correction. Atmospheric pressure does NOT use the station `Z`.**

Every consumer of `tsd_weather[...].xyz[2]` in the pinned tree:

| Location | Kind | Uses `Z` numerically? |
| --- | --- | --- |
| `SHUD/src/classes/ForcingProvider.hpp:63` | accessor (`z()`) | no (pass-through) |
| `SHUD/src/ModelData/MD_readin.cpp:395` | diagnostic `printf` of `xyz[0..2]` | no |
| `SHUD/src/ModelData/MD_ET.cpp:32` | **temperature lapse correction** | **YES** |

The single numerical consumer, `SHUD/src/ModelData/MD_ET.cpp:32` inside
`Model_Data::tReadForcing`:

```cpp
t0        = tsd_weather[idx].getX(t, i_temp);
t_temp[i] = TemperatureOnElevation(t0, Ele[i].z_surf, tsd_weather[idx].xyz[2]) + gc.cTemp;
```

The correction function, `SHUD/src/Equations/Equations.hpp:66-72`:

```cpp
double TemperatureOnElevation(double t, double Zi, double Zt){
    if( ifequal(Zi, NA_VALUE) || ifequal(Zt, NA_VALUE) ){
        return t;
    }else{
        return t + (Zt - Zi) * dTdZ;
    }
}
```

with constants `SHUD/src/Model/Macros.hpp:83` `#define NA_VALUE -9999` and
`SHUD/src/Model/Macros.hpp:50` `#define dTdZ 0.0065  /* Adiabatic Lapse Rate 6.5 [K/km] */`.

Binding of arguments at the call site:

- `t`  = `t0` — raw temperature read from the forcing series.
- `Zi` = `Ele[i].z_surf` — the **element (triangle centroid) surface elevation**,
  derived from the mesh node elevations (`z_surf = (zmax1 + zmax2 + zmax3) / 3.0`,
  per the pinned tree's own commit history), i.e. from `.sp.mesh`/`.sp.att`, NOT from
  forcing.
- `Zt` = `tsd_weather[idx].xyz[2]` — the **forcing station `Z`** from the `.tsd.forc`
  station row.

So station `Z` (`Zt`) drives a standard environmental-lapse downscaling of temperature
from the forcing-station elevation to each element's elevation, magnitude
`(Zt - Zi) * 0.0065 [K]`.

Pressure is elevation-corrected too, but from the **element** elevation, not the
station `Z`: `SHUD/src/classes/Element.cpp:132`

```cpp
FixPressure = PressureElevation(z_surf);   // z_surf = element elevation, not station Z
```

(`PressureElevation` at `SHUD/src/Equations/is_sm_et.hpp:92`). `FixPressure` then feeds
`PsychrometricConstant` and `AirDensity` in `MD_ET.cpp:56,61` — all off the element
elevation. Station `Z` never enters the pressure path.

## Q3 — Production `.cfg` correction-switch status

**Answer: the temperature lapse correction is UNCONDITIONAL in the solver — there is
no `.cfg`/config switch that gates it. The only disable path is data-level: a station
`Z` equal to `NA_VALUE` (`-9999`) short-circuits the correction.**

- The `TemperatureOnElevation` call in `tReadForcing` (`MD_ET.cpp:26-32`) is executed
  every timestep for every element with no surrounding `if` on any config switch. The
  only config switch in `MD_ET.cpp` is `CS.cryosphere` (`MD_ET.cpp:125`), which gates a
  separate cryosphere branch, not the lapse correction.
- `gc.cTemp` (added after the correction on `MD_ET.cpp:32`) is an **additive
  calibration constant**, not a switch — it cannot disable the lapse term.
- Consequently the deployed per-basin `.cfg` files (a node-22/node-27 artifact) cannot
  turn this correction off; behavior is governed **solely by the station `Z` value**
  the `.tsd.forc` carries. Grepping `SHUD/src/classes/ModelConfigure.cpp` for a
  temperature/elevation/lapse correction key returns nothing.

Corroborating live-baseline fact (docs §附录 A): `zhaochen_mc` ships stations with
`Z=-9999`, which means those stations already hit the `NA_VALUE` branch and skip the
lapse correction **today**; basins whose CMFD station rows carry real elevations (e.g.
`zhaochen_wem`, "真实高程") DO get lapse-corrected today. The baseline behavior is thus
mixed and is driven entirely by the station `Z` value — confirming Q3.

---

## Reasoning → verdict

1. Docs §7.5 / §P0.3 rule (verbatim intent): "只有确认 `Z` 不参与数值计算后，才允许使用
   声明过的 `sentinel`。否则必须使用明确的 elevation source。" — sentinel is permitted
   **only if** station `Z` is proven **not** to participate in numerical computation;
   otherwise an explicit elevation source is **required**.
2. Q1–Q2 prove station `Z` **does** participate (temperature lapse). The §7.5 sentinel
   pre-condition is therefore **not** met.
3. The solver's `NA_VALUE` branch means a `sentinel` verdict (emit `z = -9999`) would be
   *safe* in the narrow sense of not producing NaNs — but it would **silently drop the
   temperature elevation downscaling** the solver otherwise applies. Over ~25 km IFS/GFS
   cells that span large intra-basin elevation ranges, dropping lapse is a scientific
   regression versus the CMFD baseline (which applied lapse wherever station `Z` was
   real). That fails both the §7.5 rule and the "don't change hydrologic behavior under
   the pilot" posture. `sentinel` is rejected.
4. `canonical_orography` (the met product's own orography — the elevation at which the
   gridded temperature is physically valid) is the **ideal** `Zt`. But the Change 2
   registry schema (docs §5.1 field list; `met.canonical_grid_snapshot` /
   `met.canonical_grid_cell` per `canonical-source-grid-registry` Task 2.1) stores
   lon/lat/ordinal but **no orography/elevation** column. It is not available to the
   builder today. Deferred as the preferred upgrade once the registry carries
   per-cell orography.
5. `model_dem_at_cell_center` is an **explicit elevation source** that (a) satisfies the
   §7.5 requirement, (b) preserves lapse downscaling (behavior continuity with the CMFD
   baseline), and (c) is derivable from assets the builder already reads — the model DEM
   (mesh node elevations) sampled at the registered grid-cell center. **Selected.**

## Consumption contract (how the builder uses this verdict)

- The verdict is the single `z_policy` authority for
  `workers/mapping_builder/binding.py` (the `ZPolicy` struct at
  `binding.py:955`, whose `policy_name` MUST be one of `ALLOWED_Z_POLICIES` at
  `binding.py:250` — `{canonical_orography, model_dem_at_cell_center, sentinel}` — and
  which requires a non-empty provenance checksum at `binding.py:984`).
- The builder binds this verdict-evidence file's checksum into the existing
  `ZPolicy.readiness_manifest_checksum` provenance slot (the field is not renamed —
  see design.md "Naming debt" note; renaming the code field is a deferred follow-up).
- Because the verdict is `model_dem_at_cell_center` (an explicit source), the builder
  MUST populate `per_cell_z` for every used cell from the model DEM at the cell center;
  a missing per-cell `z` fails closed (`ZPolicyCellMissingError`,
  `binding.py:750`) — the builder never invents a numeric default.

## Deviation note

The change task brief referenced the readiness change as "readiness #895". The
readiness change is **Epic #886** (`cmfd-direct-grid-platform-readiness`; confirmed via
`docs/stage-pipeline-log.jsonl` and `workers/mapping_builder/binding.py` docstrings,
which cite "Epic #886"). `#895` does not correspond to that change. This verdict and
the spec delta use **#886** to match the code and logs.
