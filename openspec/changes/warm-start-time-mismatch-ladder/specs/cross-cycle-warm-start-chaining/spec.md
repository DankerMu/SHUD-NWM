# cross-cycle-warm-start-chaining (delta)

## MODIFIED Requirements

### Requirement: Warm-start IC is produced at the next cycle's init time

A production forecast cycle SHALL run SHUD for the full product horizon and preserve selected
T+6/T+12 checkpoint states from the same long run, so successor cycles can initialize from a SHUD
initial-condition snapshot valid at their init time. `Update_IC_STEP` is a checkpoint write cadence,
not a shortened forecast horizon or a request for extra short production runs.

#### Scenario: Saved snapshot is keyed at the next cycle init time
- **WHEN** the forecast long run for cycle N writes a checkpoint whose header time equals `T_{N+1}`
- **THEN** the saved snapshot has `valid_time == T_{N+1}` and is normalized to a canonical
  `state.cfg.ic` recording the original SHUD filename
- **AND** the forecast run still has `end_time == T_N + forecast_horizon_hours`.

#### Scenario: Three-way time consistency
- **WHEN** cycle N+1 consumes the saved state
- **THEN** the snapshot `valid_time`, the `.cfg.ic` header minute-time, and the run's
  `start_time`/`cycle_time` all equal `T_{N+1}`
- **AND** a mismatch among the three is a blocker at the **candidate-snapshot** level — the
  drifted candidate is rejected and is never consumed at the wrong time; on the degradation
  terminals (next usable state, labeled cold start) the rejection is recorded through the
  corrupted-state channel, while the systemic-escalation and exact-warm-start terminals fail the
  run loudly without persisting the mark (escalation deliberately keeps the snapshots usable so
  the alarm repeats). The run-level terminal is governed by the forecast-warm-start
  degradation-ladder requirement, so a rejected candidate no longer implies a whole-run failure
  outside the exact-warm-start policy.
