## Why

The scheduler state-index copyback replay audit identifies pre-commit refusal
reasons with source line numbers. Unrelated insertions move those lines, and the
index has already drifted while also omitting two copyback lock reasons. A stale
index gives reviewers false confidence without changing runtime behavior.

## What Changes

- Replace line-number citations with a reason-to-one-or-more-owning-functions mapping.
- Preserve every raise-point owner represented by the old index, including seven reasons with multiple owners and both copyback lock reasons.
- Enforce exact reason-key equality, non-empty owners, known multi-owner sets, and reason literals in each named owner function.
- Preserve replay allowlist membership, classification, exit codes, and receipts.

## Capabilities

### New Capabilities

- `stable-replay-reason-ownership`: Replay refusal reasons are auditable through stable function identities.

### Modified Capabilities

None.

## Impact

Affected files are the replay tool's audit metadata and its focused tests. No
scheduler state transition, allowlist, database, Slurm, or receipt behavior
changes.
