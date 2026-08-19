## ADDED Requirements

### Requirement: The manual-retry durable-success set is named distinctly from the pipeline durable-success set

The manual-retry refusal predicate SHALL consume a status set — on both
the DB-backed path and its file-journal twin — whose name is distinct from
`scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`, because the two
sets deliberately differ in membership (`"complete"` counts as durable
success for scheduler decisions but does not block a manual retry) and a
shared name invites an accidental merge that would silently change one
predicate's behavior. The membership relationship between the two sets
SHALL be pinned by a test so that any drift on either side — or a rename
back into collision — fails loudly. This change is naming-only: neither
predicate's behavior, membership, exception shape, nor caller surface
changes.

#### Scenario: the membership divergence is explicit and locked

WHEN the manual-retry set and the scheduler durable-success set are
compared
THEN the manual-retry set equals exactly `{"succeeded", "parsed",
"published"}`, the scheduler set equals exactly `{"succeeded", "parsed",
"published", "complete"}` (pinned separately, so collapsing the scheduler
set down to three members — the one merge direction that would change
behavior — also fails), the manual-retry set equals the scheduler set
minus `"complete"`, and a regression test asserts all three relationships

#### Scenario: manual retry behavior is unchanged

WHEN a run's durable hydro status is one of the three manual-retry
members
THEN both manual-retry paths continue to refuse the retry exactly as
before the rename — the DB lane pinned by its existing parametrized
refusal test, the file-journal lane pinned by a new refusal-arm test
(that arm had no coverage before this change); and `"complete"` — absent
from the manual-retry set and unreachable on the DB lane (it is not a
`hydro.run_status` enum value) but representable on the file-journal
lane — continues not to trigger the refusal, asserted both on the
file-journal lane and at the constant level
