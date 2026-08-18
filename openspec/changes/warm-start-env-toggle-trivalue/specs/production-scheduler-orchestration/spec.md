## ADDED Requirements

### Requirement: An unreadable warm-start env toggle never enables the terminal-skip shortcut

The scheduler SHALL treat the `NHMS_REQUIRE_FORECAST_WARM_START` compat
toggle as three-valued — explicitly enabled, explicitly disabled
(including unset, which parses to the default of disabled), or unreadable
(the orchestrator env config failed to parse for any reason, related to
the flag or not) — and SHALL allow the completed-cycle terminal-skip
shortcut only when the toggle is explicitly disabled, because collapsing
"the check could not be completed" into "the check answered no" silently
skips the §8 gating and packaged-IC evidence pass for every
journal-complete cycle while the operator believes the toggle is on, and
leaves the underlying env typo invisible. An unreadable toggle takes the
strict warm-start evidence path (the cost is one extra §8 check, never a
lost audit trail) and logs one
`SCHEDULER_WARM_START_ENV_UNREADABLE` warning per scheduler instance
carrying the parse error so the root-cause env can be found from the
log. Explicit values preserve today's behavior byte-for-byte: explicitly
disabled plus a durably-complete pipeline still terminal-skips (the D8.9
compat flow), and explicitly enabled still emits §8 evidence.

#### Scenario: an unrelated env typo no longer silently drops §8 evidence

WHEN the orchestrator env config fails to parse (for example an unrelated
`FORECAST_HORIZON_HOURS=abc`) while a candidate's pipeline is
journal-complete
THEN the strict warm-start evidence path executes and returns evidence
instead of the terminal-skip shortcut returning nothing, and one warning
naming the parse failure is logged for the scheduler instance

#### Scenario: explicit values keep the compat behavior

WHEN the toggle parses successfully
THEN explicitly disabled (or unset) plus a journal-complete pipeline
still takes the terminal-skip shortcut, explicitly enabled still takes
the strict path, and no unreadable-toggle warning is logged
