## Context

Issue #1619 is audit metadata drift. Numeric source positions are not stable
identities, and one reason can be emitted by more than one merge-path function.
The repair is independently mergeable because it does not alter the runtime
reason set or replay control flow.

## Goals / Non-Goals

**Goals:**

- Give every pre-commit reason one or more stable owning function names.
- Preserve all known function-level raise sites from the replaced index.
- Make drift fail focused tests instead of silently staling documentation.

**Non-Goals:**

- Do not change `_PRE_COMMIT_INDEX_REASONS`.
- Do not change replay allowlists, classifications, exit codes, receipts, or scheduler behavior.
- Do not keep parallel numeric line citations.

## Decisions

1. Store ownership as `Mapping[str, tuple[str, ...]]`; a singleton still uses a tuple so empty/multi-owner completeness has one shape.
2. Compare mapping keys exactly with `_PRE_COMMIT_INDEX_REASONS` and require every tuple to be non-empty.
3. Pin the seven known multi-owner reason sets independently. This prevents a superficially complete single-owner compression from discarding old audit information.
4. Parse `packages/common/state_manager.py` and require every mapped function to contain the reason literal in its body. A rename/move or changed raise site requires an intentional mapping update.

## Invariant Matrix

- Governing invariant: every runtime pre-commit reason SHALL have complete stable source ownership metadata.
- Producer: reason literals emitted by named state-manager functions.
- Validator: exact key/non-empty/multi-owner/literal focused tests.
- Consumer: copyback replay audit and reviewers; runtime classification reads the unchanged reason set.
- Failure paths: new reason, removed reason, renamed owner, emptied owner tuple, collapsed multi-owner row.
- Evidence: focused replay suite plus strict OpenSpec validation.

## Risks / Trade-offs

- Function names can change, but a failing ownership test makes that change explicit.
- AST/literal checks prove declared ownership, not arbitrary semantic reachability; exact known multi-owner sets preserve the replaced audit's observed coverage.

## Migration Plan

1. Replace the old line-number table with function-owner tuples.
2. Add exact key, non-empty, seven-row, and literal ownership tests.
3. Run the focused replay suite and repository lint/spec gates.
4. Roll back by reverting the metadata/test commit; no persisted data migration exists.

## Open Questions

None.
