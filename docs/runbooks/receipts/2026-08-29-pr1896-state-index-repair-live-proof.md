# PR #1896 state-index repair live proof

Date: 2026-08-29
Frozen implementation SHA: `8236614926a07eb3febd93220a5df5d11dc07352`
Issues: #1204, #1482

## Scope

This receipt proves the focused backend suite on node-27 and the operator CLI on
node-22 against disposable state-index roots. It did not read or write either
production state index and did not submit Slurm work.

Both active remote checkouts were on unrelated work with untracked files, so the
verification used detached clean worktrees at the frozen SHA. No active branch,
stash, or untracked evidence was modified.

## Node-27 backend oracle

- Detached worktree: `/home/nwm/NWM-pr1896-validate`
- Python: `3.11.15`
- Environment reuse: the active checkout's existing `.venv`, invoked with
  `uv run --no-sync --active`; no sync or environment rebuild
- Command:

  ```bash
  uv run --no-sync --active pytest -q \
    tests/test_state_manager.py \
    tests/test_scheduler_state_index_repair.py \
    tests/test_scheduler_state_index_copyback_replay.py
  ```

- Result: `202 passed in 10.40s`

No live DB/display receipt applies: this PR changes only DB-free file-state and
operator tooling.

## Node-22 disposable two-lane oracle

- Detached worktree: `/scratch/frd_muziyao/NWM-pr1896-validate`
- Interpreter: exact active
  `/scratch/frd_muziyao/NWM/.venv/bin/python`, Python `3.12.7`
- Environment discipline: no `uv sync`, no bare `uv run`, no `.venv` change
- Disposable roots:
  - reference: `/scratch/frd_muziyao/pr1896-state-index-verify-82366149/reference`
  - destination: `/scratch/frd_muziyao/pr1896-state-index-verify-82366149/destination`
  - private archives/receipts under the same disposable root
- Operation: destination-only `recompute-checksum`; reference and destination
  contained distinct unrelated entries, and only the destination checksum was
  corrupted.

Evidence:

1. Dry-run reported `destination.checksum_valid=false`,
   `reference.action=skip`, `mutation_started=false`, and `status=preview`.
2. Full disposable-tree fingerprints before and after dry-run were byte-identical:
   both evidence files have SHA-256
   `0ec7a03bba3ad07b39c5b4fa83c2c74bad155a4d491ac4f99b58f91fc8c231b5`.
3. Enforce reported `status=repaired`, destination `committed=true`, and
   reference `untouched_reason=validated_read_only_sibling`.
4. The archived destination pre-image digest equals the intentionally corrupted
   destination digest:
   `0eecfb831a2a06ae2d97bacca6157883d2e62c577e26830c8695016a4cc409d1`.
5. Independent validation proved:
   - repaired destination checksum passes the production validator;
   - destination entry count remains `2` and entry mappings/order are unchanged;
   - reference bytes remain unchanged, SHA-256
     `5ea9583182e000d6739eb8c342b5098701088fd61f4d6850f1dbbb9343389e1d`;
   - receipt schema is `nhms.scheduler.state_index_repair_receipt.v1`.

The complete local-only evidence bundle and checksums are retained under
`.workplans/1204-1482/remote/node22/`; this committed receipt is the
secret-safe durable summary.

## Result

PASS. Tasks 4.5 and 4.6 of `scheduler-state-index-repair` are satisfied at the
frozen implementation SHA.
