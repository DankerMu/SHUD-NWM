## ADDED Requirements

### Requirement: Recalibration state continuity routes by state-compatibility fingerprint

The mechanism SHALL admit a declared warm carry-over, without a cold start, for
a `M1→M1′` package update whose only hydrologic-core change is calibration
parameters. Eligibility SHALL be decided by a **state-compatibility
fingerprint**: the same domain-separated fingerprint computation, restricted to
the eight surfaces `geol`, `lake`, `land`, `mesh`, `river`, `soil`,
`sp_att_non_forc`, `state_schema` — that is, the ten `hydrologic_core_fingerprint`
surfaces minus `calibration` and `solver_config`.

This route SHALL be opt-in and explicitly declared: the clone mechanism SHALL
take a `transfer_mode` whose default is `fix_forward`, and the ten-surface
`hydrologic_core_fingerprint` route SHALL remain the default and SHALL be
unchanged. Only `transfer_mode='recalibration'` engages the eight-surface gate.

When the eight state-compatibility surfaces are equal the clone SHALL proceed
and the clone row SHALL record `clone_gate_kind='state_compatibility'` together
with the accepted eight-surface fingerprint value. When any of the eight
surfaces differs — including a hydrologic-core file present on exactly one side
— the clone SHALL be refused fail-closed with no state row written, surfacing
the stable error code `state_clone_cold_start_approval_required` and the
refusal scope `state_compatibility_unequal`.

The gate inputs SHALL be read **per side**: the `state_schema` surface SHALL
hash `M1`'s own `cfg.ic` bytes against `M1′`'s own `cfg.ic` bytes. Supplying one
side's bytes to both sides is a false pass and SHALL NOT be the recalibration
route's behavior.

The spin-up-distortion announcement obligation SHALL be retained: a warm
carry-over across a recalibration is lower distortion than a cold start but is
not zero distortion, and the clone receipt is the declared carry-over evidence
that records it.

#### Scenario: Calibration-only update carries state over

- **WHEN** an `M1→M1′` update changes only `cfg.calib`, `CALIB/*`, and
  `cfg.para`, leaving mesh, river, soil, geol, land, lake, `.sp.att` non-`FORC`
  fields and `cfg.ic` byte-identical, and the clone is requested with
  `transfer_mode='recalibration'`
- **THEN** the eight-surface state-compatibility fingerprints are equal and the
  `(M1, source, t*) → (M1′, source, t*)` clone proceeds with no physical state
  file copied
- **THEN** the clone row records `clone_gate_kind='state_compatibility'`,
  `clone_gate_fingerprint` equal to the accepted eight-surface value,
  `cloned_from_model_id = M1`, and `model_package_version` /
  `model_package_checksum` equal to `M1′`'s
- **THEN** the ten-surface `hydrologic_core_fingerprint` route is not consulted
  and its behavior for every other caller is unchanged.

#### Scenario: A new cfg.ic alongside the recalibration refuses the carry-over

- **WHEN** an `M1→M1′` update changes calibration parameters and **also** ships
  a different `cfg.ic`, and the clone is requested with
  `transfer_mode='recalibration'`
- **THEN** the `state_schema` surface — hashed from each side's own `cfg.ic`
  bytes — differs, the clone is refused fail-closed with no row written, and
  the refusal surfaces `state_clone_cold_start_approval_required` with
  `refusal_scope='state_compatibility_unequal'`
- **THEN** the refusal is correct behavior, because a new initial-condition
  file declares a new modeling starting point.

#### Scenario: A hydrologic-core file added or removed on one side refuses

- **WHEN** a recalibration candidate has a `*.lake.*` file present in `M1` but
  absent in `M1′` (or the reverse), while every other surface matches
- **THEN** the enumeration covers files from both packages, the absent side
  fails to resolve the file, and the clone is refused fail-closed with
  `refusal_scope='state_compatibility_unequal'`
- **THEN** no clone row is written for `M1′` under either state index.

#### Scenario: Fix-forward remains the default and is unchanged

- **WHEN** any existing caller invokes the clone without naming a
  `transfer_mode`
- **THEN** the ten-surface `hydrologic_core_fingerprint` gate runs exactly as
  before, all six existing refusal scopes remain reachable with their existing
  meanings, and the clone row records `clone_gate_kind='hydrologic_core'`
- **THEN** the recalibration route adds no behavior to the default path.
