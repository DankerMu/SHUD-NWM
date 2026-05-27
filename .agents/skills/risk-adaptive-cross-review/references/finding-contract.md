# Finding Contract

Cross-review findings must be actionable enough to drive a fix without another
round of interpretation.

## Severity

| Severity | Meaning | Gate |
| --- | --- | --- |
| P0 | Security hole, data loss, broken core behavior, invalid spec fixture | Must fix |
| P1 | Correctness, contract, integration, migration, or evidence gap likely to break users or implementation | Should fix before merge |
| P2 | Meaningful maintainability, coverage, or consistency issue that may compound | Usually fix or explicitly defer |
| Note | Non-blocking observation, polish, or unclear concern | Does not block |

## Required Fields

P0/P1/P2 findings must include:

- **Severity**
- **Failure class**: e.g. `path-safety`, `schema-contract`, `state-transition`, `test-evidence`, `task-boundary`
- **Violated contract or invariant**
- **Evidence**: file path, line, diff hunk, spec requirement, issue text, command output, or artifact section
- **Concrete scenario**: how the issue fails or becomes ambiguous
- **Consequence**
- **Fix direction**
- **Required verification**: test, command, spec change, or evidence update
- **Sibling surfaces to audit**, if any
- **Blocking status**

## Non-Blocking Notes

Keep a concern non-blocking when it lacks a concrete scenario, cannot name the
violated contract, or is a style preference without behavioral or governance
risk. Do not hide clear bugs as questions.

## Downgrading

You may downgrade a reviewer concern only with explicit rationale:

- the cited behavior is outside scope or a documented non-goal
- the scenario is impossible under the artifact's stated preconditions
- the finding duplicates another stronger finding
- the required change would expand scope beyond the accepted fixture
