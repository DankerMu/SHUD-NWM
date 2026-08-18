## ADDED Requirements

### Requirement: Repeated identical no-progress reasons open a cross-pass evidence circuit

The scheduler SHALL detect, across consecutive one-shot passes, a subject
that keeps reporting the same no-progress reason, and surface it as an
observe-only circuit marker — because the repeating shapes that motivated
this requirement (a permanently-classified failure re-judged every pass,
a deliberately non-convergent held reservation, a predecessor-pending
stall) never touch any retry counter, so without cross-pass aggregation
they repeat silently until a human happens to look. When enabled with
threshold N (default 3; a non-positive threshold disables the feature
entirely, byte-for-byte preserving today's behavior — no state file, no
payload key, no log line), the tracker persists its state in a JSON file
under the evidence root (surviving the one-shot process model; a missing
or corrupt state file resets to empty with a `state_reset` marker and
never fails the pass), observes each pass's already-assembled evidence
payload through three adapters (candidate summaries with a non-advancing
status and non-empty reason; candidate state evidence flagged
`operator_action_required`; reserved-unbound reconcile outcomes keyed by
action and reason class), and applies strictly-consecutive semantics: the
same (subject, reason) pair increments, a changed reason resets the
count, and a subject absent from a pass is cleared. A pair reaching N
consecutive passes appears in the pass evidence under a top-level
`no_progress_circuit` block (open entries capped at 50 with a truncation
count, so the block stays bounded under the evidence byte budget) and in
one aggregated `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN` warning per pass.
The circuit is evidence only: it never alters scheduling decisions,
retries, terminal statuses, or the closed reconciliation vocabularies.

#### Scenario: the same reason repeating across passes opens the circuit

WHEN a subject reports the identical no-progress reason in N consecutive
one-shot passes over a shared evidence root
THEN the Nth pass's evidence carries a `no_progress_circuit.open` entry
for that (subject, reason) pair with its consecutive-pass count and
first/last pass identifiers, and the pass logs one aggregated
circuit-open warning

#### Scenario: progress or change breaks the streak

WHEN the subject reports a different reason, disappears from a pass, or
the pass is healthy
THEN the count resets (changed reason), the entry is cleared (absence),
or no observation is produced at all (healthy pass), and the block's
open list stays empty with no warning logged

#### Scenario: disabling the feature preserves today's behavior

WHEN the configured threshold is zero or negative
THEN no state file is read or written, the evidence payload gains no new
key, and no circuit log line is emitted

#### Scenario: the tracker survives the one-shot process model

WHEN each pass runs in a fresh scheduler process against the same
evidence root
THEN the consecutive count accumulates across processes via the persisted
state file, and a corrupt or missing state file resets counting to empty
with a `state_reset` marker instead of failing the pass
