# forecast-warm-start (delta)

## ADDED Requirements

### Requirement: Time-Mismatch Snapshots Join the Corrupted-State Degradation Ladder

The runtime SHALL treat a warm-start candidate snapshot rejected for a
header/valid-time mismatch as one more corrupted snapshot on the
degradation ladder rather than a whole-run failure, except under the
exact-warm-start policy: the candidate is marked corrupted with a
message carrying the greppable `WARM_START_TIME_MISMATCH:` token
(distinguishable from the header-shape rejection channel), the ladder
moves to the next usable state, and when no usable state remains the
run falls back to the labeled cold start — unless every rejection in
the run was a time mismatch and there were at least two of them, in
which case the run SHALL fail with the dedicated
`WARM_START_TIME_MISMATCH_SYSTEMIC` error code after clearing the
staged initial states, preserving the fail-loud signal for systematic
time drift. Under the exact-warm-start policy the historical behavior
is kept byte-for-byte: the original `WARM_START_TIME_MISMATCH` error
propagates and the run fails without degradation. The time-consistency
criterion itself, the checksum and header-shape rejection channels, and
the legitimate stale-reuse path (an older state whose header matches
its own valid time) keep their existing behavior byte-for-byte.

#### Scenario: A single drifted snapshot no longer kills the cycle

- **GIVEN** a warm-start run whose selected snapshot's native IC header
  minute disagrees with its recorded valid time, with a usable healthy
  snapshot available next in the state index
- **WHEN** the runtime stages the initial state
- **THEN** the drifted snapshot is marked corrupted with the
  `WARM_START_TIME_MISMATCH:` token in its message, the ladder selects
  the healthy snapshot, and the run proceeds warm (`init_mode=3`)

#### Scenario: A lone drifted snapshot degrades to labeled cold start

- **GIVEN** the same drifted snapshot with no other usable state
- **WHEN** the runtime stages the initial state
- **THEN** the run falls back to `cold_start_no_state` instead of
  failing, and no staged `*.cfg.ic` file remains in the input workspace

#### Scenario: Unanimous plural time mismatches escalate loudly

- **GIVEN** a state index whose every candidate (at least two) is
  rejected for a time mismatch
- **WHEN** the ladder exhausts
- **THEN** the run fails with `WARM_START_TIME_MISMATCH_SYSTEMIC`, the
  message is greppable from run evidence and carries the rejection
  counts, and the staged initial states are cleared — and the rejected
  snapshots are NOT persistently marked unusable on this exit, so the
  next cycle walks the ladder again and raises the same systemic signal
  instead of silently cold-starting: the systematic-drift alarm rings
  every cycle until the drift is fixed

#### Scenario: Mixed rejection causes keep the cold-start fallback

- **GIVEN** a ladder exhausted by a mixture of checksum and
  time-mismatch rejections
- **WHEN** the ladder exhausts
- **THEN** the run falls back to `cold_start_no_state` exactly as today
  — escalation requires unanimity

#### Scenario: Exact warm-start keeps its historical failure byte-for-byte

- **GIVEN** a run under the exact-warm-start policy whose selected
  snapshot has a time mismatch
- **WHEN** the runtime stages the initial state
- **THEN** the original `WARM_START_TIME_MISMATCH` error propagates and
  the run fails without degradation — error code, message and traceback
  identical to the pre-change behavior — while the staged initial
  states are now cleared first, matching the workspace hygiene the
  checksum arm of the exact-warm policy already had
