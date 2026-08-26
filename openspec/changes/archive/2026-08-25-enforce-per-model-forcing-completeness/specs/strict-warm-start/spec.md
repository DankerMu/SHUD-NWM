# strict-warm-start Specification Delta

## ADDED Requirements

### Requirement: A strict-warm-start restart at forecast SHALL consult the per-model forcing witness

A candidate state decision carrying `restart_stage: "forecast"` SHALL NOT be
emitted before the per-model forcing witness has been consulted for that
candidate's own `(source, cycle, basin_version_id, model_id)`. Restarting at
`forecast` skips the forcing stage, so an unconsulted witness is a forecast
submitted against a package that may not exist. When the witness reports the
package absent, the candidate MUST be blocked with the witness's named reason
instead of restarted at `forecast`.

This applies to every strict-warm-start decision that carries
`restart_stage: "forecast"`, including the terminal init-state mismatch, the
terminal run-manifest missing, and the run-manifest mismatch legs.

#### Scenario: A re-identified model with no forcing of its own is blocked, not restarted at forecast

- **GIVEN** a candidate whose `model_id` differs from the one that completed this
  cycle's forcing, on the same `basin_id`, `cycle_time` and `source_id`
- **AND** no forcing package exists under the candidate's own `model_id`
- **WHEN** the strict-warm-start reconcile would restart it at `forecast`
- **THEN** the decision emitted is `blocked`, carrying the witness's reason
- **AND** no forecast job is submitted for that candidate
- **AND** the candidate's evidence carries a `forcing_provenance` annotation
  naming the tier that failed to witness the package

#### Scenario: A strict-warm-start restart whose own forcing exists is unaffected

- **GIVEN** a candidate that the strict-warm-start reconcile would restart at
  `forecast`
- **AND** a forcing package exists under that candidate's own `model_id` for this
  cycle
- **WHEN** the decision is emitted
- **THEN** it is the same retry decision, with the same `restart_stage`, that it
  was before this requirement
- **AND** its evidence carries a `forcing_provenance` annotation recording the
  witnessing tier

#### Scenario: The witness annotation is recorded even when it does not block

- **WHEN** the per-model forcing witness is consulted on a strict-warm-start
  decision
- **THEN** the provenance annotation is recorded on the candidate's evidence
  regardless of whether the decision was blocked
- **AND** an operator reading the pass evidence can tell which tier witnessed the
  package without re-deriving any path
