## ADDED Requirements

### Requirement: Terminal state map covers the terminal-state vocabulary and empty states normalize safely

`SLURM_STATE_MAP` SHALL contain every state that
`services/production_closure/slurm_validation.py` `TERMINAL_SLURM_STATES`
enumerates (including `REVOKED` and `SPECIAL_EXIT`, mapped to FAILED), so the
default-less file-cohort task projection cannot strand a cohort in
`task_accounting_incomplete` on a terminal state; this registration is
orthogonal to `map_slurm_error_code`, whose deliberately-unmapped verdict for
these states (falling to `SLURM_JOB_FAILED`) is unchanged. State
normalization SHALL treat empty or whitespace-only raw states as the existing
`UNKNOWN` fallback instead of raising, in both the gateway and the
production-closure sibling copy.

#### Scenario: REVOKED or SPECIAL_EXIT array task projects failed, cohort stays accountable

WHEN a file-cohort array task's sacct raw state is REVOKED or SPECIAL_EXIT
THEN the task projection reports outcome failed with accounting complete
AND the cohort outcome action is terminal, not task_accounting_incomplete

#### Scenario: empty sacct State field converges to UNKNOWN, not IndexError

WHEN a sacct row passes field-count validation but carries an empty or
whitespace-only State field
THEN state normalization returns UNKNOWN on every parse leg (status, list,
array-member aggregation) and no bare IndexError escapes the gateway contract

#### Scenario: terminal-state vocabulary cannot drift apart again

WHEN TERMINAL_SLURM_STATES gains a state absent from SLURM_STATE_MAP
THEN a committed meta assertion fails
