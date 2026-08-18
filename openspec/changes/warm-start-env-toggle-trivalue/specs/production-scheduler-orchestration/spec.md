## ADDED Requirements

### Requirement: An unreadable warm-start env toggle never enables the terminal-skip shortcut

The scheduler SHALL treat the `NHMS_REQUIRE_FORECAST_WARM_START` compat
toggle as three-valued — explicitly enabled, explicitly disabled
(including unset, which parses to the default of disabled), or unreadable
(the orchestrator env config failed to parse for any reason, related to
the flag or not) — and SHALL allow the completed-cycle terminal-skip
shortcut only when the toggle is explicitly disabled, because collapsing
"the check could not be completed" into "the check answered no" silently
short-circuits the §8 gating decision for a journal-complete cycle and
leaves the underlying env typo unattributable: on the db-free main path
the pass then crashes at some later unguarded env read with no clue which
variable failed, and on the predecessor-backfill path the error is
swallowed entirely. An unreadable toggle first logs one
`SCHEDULER_WARM_START_ENV_UNREADABLE` warning per scheduler instance
carrying the parse error (the root-cause env is readable straight from
the log), then takes the strict warm-start path. On the strict-path
branches that read the env again (the legacy landing and the
warm-continue / blocked-predecessor tail) the same parse failure
re-raises — a loud, attributable failure consistent with how every other
`OrchestratorConfig.from_env()` call site propagates; the early-return
decision branches that never read the env return their evidence with
only the warning. Either shape is acceptable and neither is a silent
skip; no degraded parallel mode is invented for the unreadable state. On the
backfill path the same change turns the swallowed error into a
`predecessor_gate_failed` skip with the warning already logged —
fail-closed instead of silently admitting. Explicit values preserve
today's behavior byte-for-byte: explicitly disabled plus a
durably-complete pipeline still terminal-skips (the D8.9 compat flow),
and explicitly enabled still emits §8 evidence with no new logging.

#### Scenario: an unrelated env typo fails loudly and attributably instead of silently skipping

WHEN the orchestrator env config fails to parse (for example an unrelated
`FORECAST_HORIZON_HOURS=abc`) while a candidate's pipeline is
journal-complete on the db-free path
THEN the terminal-skip shortcut is not taken, the strict warm-start path
is entered and — on a branch that reads the env again — the parse
failure surfaces as a raised error (an early-return branch instead
returns its evidence), and one warning naming the parse failure was
logged first — the operator can read the broken variable from the log
instead of guessing

#### Scenario: the backfill path fails closed instead of silently admitting

WHEN the same unreadable env occurs on the predecessor-backfill path
THEN the strict-path error is recorded as a predecessor-gate failure
(skipping, not admitting, the predecessor) and the unreadable-toggle
warning has been logged

#### Scenario: explicit values keep the compat behavior

WHEN the toggle parses successfully
THEN explicitly disabled (or unset) plus a journal-complete pipeline
still takes the terminal-skip shortcut, explicitly enabled still takes
the strict path, and no unreadable-toggle warning is logged
