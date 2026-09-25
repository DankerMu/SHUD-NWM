## ADDED Requirements

### Requirement: Both persistence planes SHALL normalise cloned_from_model_id identically before judging it

"Present, non-empty, and different from the row's own `model_id`" SHALL be judged on `cloned_from_model_id` with surrounding whitespace removed, where whitespace is exactly the set of characters Python's `str.isspace()` accepts. The comparison with `model_id` SHALL use the raw `model_id`. On the DB plane this SHALL be enforced by the SQL predicate of the earliest-clone-row reader, so that `LIMIT 1` never selects a row the file plane would skip. The resolver's own normalisation SHALL stay as defence in depth. The publisher's latest-clone-row reader is not governed by this requirement.

#### Scenario: A whitespace-only parent does not mask a later clone row

- **WHEN** the earliest clone row of a `(model_id, source_id)` has a `cloned_from_model_id` made only of whitespace (including tab, newline or U+3000) and a later clone row names a real predecessor
- **THEN** both planes SHALL resolve the lineage cutover from the later row

#### Scenario: A padded self-reference does not mask a later clone row

- **WHEN** the earliest clone row's `cloned_from_model_id` is the row's own `model_id` with surrounding whitespace and a later clone row names a real predecessor
- **THEN** both planes SHALL resolve the lineage cutover from the later row

#### Scenario: The planes agree

- **WHEN** the same set of clone rows is written to the file plane and to the DB plane
- **THEN** the two planes SHALL resolve the same `LineageCutover`
