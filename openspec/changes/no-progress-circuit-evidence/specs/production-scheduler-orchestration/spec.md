## ADDED Requirements

### Requirement: Repeated identical no-progress reasons open a cross-pass evidence circuit

The scheduler SHALL detect, across consecutive fully-observed passes, a
subject that keeps reporting the same no-progress reason, and surface it
as an observe-only circuit marker — because the repeating shapes that
motivated this requirement (a permanently-classified failure re-judged
every pass, a deliberately non-convergent held reservation, a
predecessor-pending stall) never touch any retry counter, so without
cross-pass aggregation they repeat silently until a human happens to
look. A fully-observed pass is one that reaches the complete-pass
evidence write; early-exit, pre-lock, lock-contended, and
resource-limit-aborted passes neither observe nor touch the persisted
tracker state, so an aborted pass can never clear accumulated counts.
When enabled with threshold N (default 3; a non-positive threshold
disables the feature entirely, byte-for-byte preserving today's behavior
— no state file, no payload key even on the bounded-compaction path, no
log line), the tracker persists its state in a JSON file under the
evidence root (surviving the one-shot process model; enabled
fully-observed passes always rewrite the file, and a missing or corrupt
state file resets counting to empty with a distinguishing `state_reset`
marker of `"missing"` or `"corrupt"` and never fails the pass), and
observes the pass's already-assembled uncompacted evidence payload
through two adapters: candidate rows from the candidate and
blocked-candidate lists whose status is `blocked` with a non-empty
reason (skipped-candidate rows are excluded because their status remains
`selected` and they include successful skips; a row flagged
`operator_action_required` in its state evidence carries that flag into
the circuit entry as an annotation), and reserved-unbound reconcile
outcome rows keyed by action and reason class, read only when the
reconcile segment completed and its outcome key is present — an
adapter whose source is absent from the pass preserves its existing
entries instead of clearing them. Counting is strictly consecutive per
subject: the same (subject, reason) pair increments, a changed reason
resets the count to one, and a subject absent while its adapter's source
is present is cleared. A pair reaching N appears in the pass evidence
under a top-level `no_progress_circuit` block (open entries capped at 50
with a truncation count) and in one aggregated
`SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN` warning per fully-observed pass.
Under evidence byte pressure the block is the first thing shed: when the
bounded payload does not fit, the whole `no_progress_circuit` block is
dropped before any pre-existing field is summarized or dropped, so every
existing compaction stage sees a payload byte-for-byte identical to one
from before this feature existed — the warning and the persisted counts
are unaffected, and the absence of the block in an over-budget pass does
not mean no circuit is open. The observation path as a whole fails open:
an unexpected observation error logs its own warning and skips the block
for that pass instead of failing the pass. The circuit is evidence only:
it never alters scheduling decisions, retries, terminal statuses, or the
closed reconciliation vocabularies.

#### Scenario: the same reason repeating across passes opens the circuit

WHEN a subject reports the identical no-progress reason in N consecutive
fully-observed one-shot passes over a shared evidence root
THEN the Nth pass's evidence carries a `no_progress_circuit.open` entry
for that (subject, reason) pair with its consecutive-pass count and
first/last pass identifiers, and the pass logs one aggregated
circuit-open warning

#### Scenario: progress or change breaks the streak, absence of the source does not

WHEN the subject reports a different reason, disappears from a pass
whose adapter source is present, or the pass is healthy
THEN the count resets (changed reason), the entry is cleared (absence
with source present), or no observation is produced at all (healthy
pass, empty open list, no warning) — while a pass whose adapter source
is itself absent (a failed reconcile segment, a dry run) and any
early-exit or aborted pass leave the persisted counts untouched

#### Scenario: disabling the feature preserves today's behavior

WHEN the configured threshold is zero or negative
THEN no state file is read or written, the evidence payload gains no new
key on either the plain or the bounded-compaction path, and no circuit
log line is emitted

#### Scenario: the tracker survives the one-shot process model

WHEN each pass runs in a fresh scheduler process against the same
evidence root
THEN the consecutive count accumulates across processes via the persisted
state file, enabled fully-observed passes always rewrite the file, and a
corrupt or missing state file resets counting to empty with the
corresponding `state_reset` marker instead of failing the pass
