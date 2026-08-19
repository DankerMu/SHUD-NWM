## ADDED Requirements

### Requirement: Orchestration dispatch failures record an attributable traceback tail in run evidence

The model run evidence SHALL carry, whenever the scheduler's
orchestration dispatch catch-all converts an unexpected exception into
per-candidate submission failures, a truncated traceback tail alongside
the sanitized error message — sufficient to attribute the raising frame
(file and line), passed through the same evidence-safety sanitization
as the message — so an occasional production failure can be located
from evidence alone instead of guessed from a bare message string.

#### Scenario: an unexpected orchestration exception is attributable

WHEN `orchestrate_cycle` raises an unexpected exception during a
scheduler pass
THEN every affected candidate's run evidence records the sanitized
message AND a truncated, sanitized traceback tail naming the raising
frame, and the evidence remains schema-compatible for existing consumers
