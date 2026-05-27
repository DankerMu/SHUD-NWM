# Failure-Class Synthesis

Synthesis turns multiple reviewer outputs into fixable work. The goal is to
close root failure classes, not respond to every comment independently.

## Steps

1. Normalize each finding into the finding contract fields.
2. Drop or downgrade vague notes that cannot be completed into the contract.
3. Merge duplicates by failure class and violated invariant.
4. Identify sibling surfaces named by reviewers or implied by the invariant.
5. Convert each failure class into one class-level fix group.
6. Attach required verification to each fix group.
7. Decide whether another cross-review is required after fixes.

## Escalation Signals

Trigger invariant-level closure when:

- two review rounds repeat the same failure class
- findings move across sibling surfaces under the same invariant
- a high-risk P0/P1 finding exposes a reusable unsafe pattern
- reviewers disagree because the fixture underspecified the contract

When this happens, do not issue narrow line-item fixes. Require an invariant
surface inventory, a regression matrix, and a fix prompt that audits analogous
paths before another cross-review.

## Fix Group Shape

```markdown
### <failure-class>

**Invariant**: <contract that must hold>
**Findings merged**: <reviewer ids / evidence refs>
**Affected surfaces**: <files/modules/specs/issues>
**Fix task**: <class-level repair>
**Verification**: <tests/commands/evidence>
**Review after fix**: yes | no, with reason
```
