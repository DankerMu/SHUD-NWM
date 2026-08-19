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
THEN the manual-retry set equals the scheduler set minus `"complete"`,
equals exactly `{"succeeded", "parsed", "published"}`, and a regression
test asserts this relationship

#### Scenario: manual retry behavior is unchanged

WHEN a run's durable hydro status is `"complete"`
THEN both manual-retry paths continue to treat it as retryable (no
refusal), exactly as before the rename
