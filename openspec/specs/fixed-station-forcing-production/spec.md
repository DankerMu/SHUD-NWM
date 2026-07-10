# fixed-station-forcing-production Specification

## Purpose
TBD - created by archiving change m23-qhh-22-production-automation. Update Purpose after archive.
## Requirements
### Requirement: Per-cycle forcing targets fixed stations
The forcing producer SHALL generate meteorological forcing for fresh cycles by mapping canonical grids to the fixed SHUD forcing stations seeded from the processed QHH package.

#### Scenario: Fixed stations selected
- **WHEN** forcing generation starts for a QHH model/cycle
- **THEN** it loads active `met.met_station` rows for the model's basin version with `station_role="forcing_grid"`
- **AND** it uses their SHUD forcing index and forcing filename metadata as the target station contract.

#### Scenario: No fixed stations blocks forcing
- **WHEN** no active forcing-grid stations exist for the QHH model/basin version
- **THEN** forcing generation fails with a missing-stations blocker
- **AND** no `met.forcing_version` is marked ready for that cycle.

### Requirement: Dynamic station timeseries are persisted
The system SHALL persist generated forcing values and provenance for each accepted model/source/cycle.

#### Scenario: Forcing version created
- **WHEN** station forcing generation completes for a canonical product
- **THEN** it writes one `met.forcing_version` linked to model, basin, source, cycle, canonical product, station count, variable set, time range, and quality metadata
- **AND** it writes `met.forcing_station_timeseries` rows for each generated station/variable/time value.

#### Scenario: Idempotent forcing generation
- **WHEN** forcing generation reruns for the same model/source/cycle/canonical identity
- **THEN** it reuses or replaces according to a deterministic idempotency policy
- **AND** it does not create duplicate ready forcing versions for the same candidate identity.

#### Scenario: Bad interpolation coverage blocks readiness
- **WHEN** canonical grids cannot cover a station or required variable/time range
- **THEN** forcing generation records the affected station/variable/time coverage gap
- **AND** downstream SHUD submission is blocked unless the policy explicitly permits reduced scope.

### Requirement: SHUD forcing package is produced
The system SHALL materialize SHUD-ready forcing files from persisted station forcing using the processed basin's file contract.

#### Scenario: SHUD forcing files written
- **WHEN** forcing version is ready
- **THEN** the runtime package contains `qhh.tsd.forc` and per-station forcing CSV/text files expected by SHUD project mode
- **AND** file paths, checksums, station count, variable count, time range, and units are recorded in the runtime manifest.

#### Scenario: rSHUD contract honored without runtime dependency
- **WHEN** SHUD forcing files are created
- **THEN** their columns, units, station ordering, and filenames follow the existing rSHUD/AutoSHUD-informed processed basin contract
- **AND** the production cycle does not call rSHUD as the hydrologic runtime solver.

### Requirement: Producer output semantics are pinned to producer_version

The forcing producer SHALL pin its output-semantics surface — `OUTPUT_UNITS`, the
precipitation conversion branch (`mm/day` accepted as factor `1.0`, any other unit
rejected), and the `rn_shortwave_factor` default — to a deterministic regression
fingerprint bound to `producer_version`. Any change to those output semantics SHALL
flip the fingerprint and fail the guard test until the developer both bumps
`producer_version` and updates the pinned fingerprint in the same change.

#### Scenario: Changing output semantics forces a producer_version bump

- **WHEN** any of `OUTPUT_UNITS`, the precipitation conversion branch behavior, or the `rn_shortwave_factor` default is changed
- **THEN** the recomputed output-semantics fingerprint MUST differ from the pinned `EXPECTED_FINGERPRINT`
- **AND** the guard test MUST fail until both `producer_version` is bumped and `EXPECTED_FINGERPRINT` is updated.

#### Scenario: Unchanged semantics keep the gate green at the pinned version

- **WHEN** the producer output semantics are unchanged
- **THEN** the recomputed fingerprint MUST equal the pinned `EXPECTED_FINGERPRINT`
- **AND** `producer_version` MUST equal the pinned value (`m2.0`).

### Requirement: OUTPUT_UNITS and manifest-unit keysets stay in lockstep

The producer's `OUTPUT_UNITS` keyset SHALL equal the manifest's
`REQUIRED_FORCING_VARIABLES` keyset, and every required forcing variable SHALL map
to a non-empty manifest unit, so that adding an `OUTPUT_UNITS` key without wiring
its manifest unit is caught by a guard test.

#### Scenario: Keyset equality and non-empty manifest units

- **WHEN** the guard test compares `set(OUTPUT_UNITS)` with `set(REQUIRED_FORCING_VARIABLES)`
- **THEN** the two keysets MUST be equal
- **AND** `package_manifest_unit(v)` MUST return a non-empty string for every required forcing variable.

### Requirement: Direct-grid mirror maintenance preserves the registration-owned active_flag

The runtime producer's direct-grid `met.met_station` mirror maintenance SHALL NOT set `active_flag=true` on either plane: the DB-plane upsert (`workers/forcing_producer/store.py:ensure_direct_grid_met_stations`) and the DB-free file plane's station-inventory handoff (`workers/forcing_producer/file_store.py:_handoff_station_rows` → `station_inventory.json` → the `met.met_station` ingest) SHALL preserve an existing row's current `active_flag` on conflict-update — never escalating `false`→`true` — and SHALL insert fresh mirror rows with `active_flag=false`. Mirror activation belongs exclusively to the cutover station-flag flip (Change 8); the writers' fail-closed derived-cache collision predicate is retained unchanged.

#### Scenario: A pre-cutover production run leaves the mirror inactive

- **WHEN** a direct-grid forcing production run executes against a registered-but-inactive variant whose registration wrote the mirror rows with `active_flag=false`
- **THEN** after the run every one of the variant's mirror rows still has `active_flag=false`
- **THEN** the shadow-window station-MVT query (`active_flag=true`) still returns only the legacy station track, so pre-cutover production cannot create a mixed display.

#### Scenario: The producer upsert never escalates active_flag

- **WHEN** the producer's mirror upsert hits an existing `met.met_station` row for the same derived-cache binding
- **THEN** the update preserves the row's current `active_flag` value (a `false` row stays `false`; a row flipped `true` by the Change 8 cutover stays `true`)
- **THEN** no code path in the producer writes the literal `active_flag=true` for the mirror, on insert or update.

#### Scenario: The file-plane handoff carries the same ownership rule

- **WHEN** the DB-free file plane emits `station_inventory.json` for the `met.met_station` handoff
- **THEN** the emitted station rows do not force `active_flag: true`, and the ingest applies the same preserve-on-update / insert-inactive rule as the DB-plane upsert
- **THEN** both planes leave mirror-activation ownership with the registration step (`active_flag=false`) and the Change 8 flip (`true`).

#### Scenario: The fail-closed collision predicate is unchanged

- **WHEN** the producer's mirror upsert targets an existing `station_id` that is not the same derived direct-grid cache binding (the conditional-update identity predicate over `station_role='direct_grid_cache'` and the `properties_json` identity fields fails)
- **THEN** the write still fails closed with the existing collision error and mutates no row
- **THEN** relaxing the flag ownership does not relax the identity collision policy (docs §7.4).

