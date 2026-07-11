# Deep Plan Structure

Use this reference when `implementation-planning` has already passed the complexity gate.

## Required Sections

Every deep implementation plan should make these sections easy to find:

- Goal
- Scope
- Not in scope
- What already exists
- Constraints
- Success criteria
- Assumptions
- Open decisions
- Phases
- Verification
- Risks
- Rollback or containment
- Next step

The plan should be grounded in real repository or system evidence, not hypothetical architecture.

Order phases decision-dense first within dependency constraints: data models, type interfaces, and user-visible flows are the parts most likely to be adjusted by a human, so they belong in early phases where review attention is highest; mechanical refactors and low-judgment work go last. Mark each phase's review attention so consumers know where to look.

## Recommended Template

```markdown
# [Topic] Implementation Plan

## Goal
[What will exist when this work is complete]

## Scope
- [In-scope item]

## Not In Scope
- [Explicit exclusions]

## What Already Exists
- [Current files, services, patterns, or workflows that shape the plan]

## Constraints
- [Timeline, compatibility, operational, or tooling constraints]

## Success Criteria
- [Observable conditions that show the work is complete]

## Assumptions
- [Assumptions that materially affect sequencing]

## Open Decisions
- [Decision still needed]

## Phases

### Phase 1: [Name]
- Outcome:
- Files / components:
- Steps:
- Verify:
- Depends on:
- Review attention: [decision-dense — review closely / mechanical — skim]

### Phase 2: [Name]
- Outcome:
- Files / components:
- Steps:
- Verify:
- Depends on:
- Review attention: [decision-dense — review closely / mechanical — skim]

## Risks
- Risk:
- Impact:
- Mitigation:

## Rollback Or Containment
- Trigger:
- Action:

## Next Step
[Approve / refine / execute]
```

## Scope-Challenge Checklist

Before finalizing the plan, pressure-test it:

- Is any phase solving a hypothetical future need rather than the confirmed task?
- Can any risky work be turned into a smaller reversible slice?
- Is there a simpler extension of existing patterns instead of a new framework?
- Are we forcing migration, rollout, or data movement into the same phase when it should be separated?
- Are there unknowns that deserve a spike before the main implementation phases?

If the answer exposes a material issue, revise the plan instead of shipping the first draft.
