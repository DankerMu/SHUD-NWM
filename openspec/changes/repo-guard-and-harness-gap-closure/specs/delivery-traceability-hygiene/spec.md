## ADDED Requirements

### Requirement: The cross-PR review-gate memory SHALL hold one authority per issue with in-vocabulary outcomes

The tracked review-gate memory file SHALL carry exactly one top-level mapping, keyed by issue, and SHALL NOT
carry per-issue entries beside it at the top level. The escalation path reads only that mapping, so a second copy
of an issue's record placed outside it is silently ignored: a ceiling that was recorded there would not raise the
human decision it exists to force, and two copies that disagree leave no way to tell which one the next run trusted.
Both observed top-level defects arrived through hand-resolved merge conflicts, not through the tool, which cannot
write a top-level key at all — so the invariant SHALL be enforced by a check the repository itself runs, on the
commits that carry the file. Where the invariant is additionally enforced closer to the writer is not constrained
here.

Every recorded closure outcome SHALL be one of the four terminal outcomes the workflow defines — merged,
superseded-by-split, abandoned, or descoped. A value outside that set is one the vocabulary does not define and
the loop-log validator rejects, so it makes the record unreadable by the same pipeline that wrote it. Because such
a value can be produced by a closure recorded without an explicit outcome, the check's failure SHALL name that
cause and the remedy, so that whoever meets it is not left to re-derive why an unremarkable-looking word is refused.

Each per-issue entry SHALL carry its ceiling list, its gate-entry count, and its closure list, each of the declared
type, so that a truncated or partially merged entry is a failure rather than a silently permissive record.

#### Scenario: A per-issue record beside the mapping is rejected

- **GIVEN** the memory file carrying an issue-keyed entry at the top level, next to the issue mapping
- **WHEN** the structural check runs
- **THEN** it fails and names the offending key, because the escalation path would not have read that entry

#### Scenario: An out-of-vocabulary closure outcome is rejected

- **GIVEN** a closure record whose outcome is not one of the four terminal outcomes
- **WHEN** the structural check runs
- **THEN** it fails and names the issue and the value

#### Scenario: An entry missing a declared field is rejected

- **GIVEN** a per-issue entry lacking its ceiling list, its gate-entry count, or its closure list
- **WHEN** the structural check runs
- **THEN** it fails rather than treating the absent field as an empty or default value
