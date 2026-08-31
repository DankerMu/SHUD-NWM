## Why

The node-22 file-backed scheduler keeps one growing `latest/`/`journal/` tree even though #1734 made hot lookups cycle-scoped. The tree is no longer a hot-path bottleneck, but it still needs a conservative disk-side boundary that cannot erase live reservation, reconcile, rollback, or older-cycle warm-start evidence.

## What Changes

- Add an opt-in node-22 retention command that plans by `(source_id, cycle_time)`, keeps 90 days hot by default, and refuses enforcement unless the configured scheduler window and a fresh pipeline frontier prove the candidate is outside the active window.
- Archive each eligible cycle as one verified, recoverable cold bundle covering its `latest/`, `journal/`, and `pipeline-events/` members before removing those members from the hot tree.
- Reuse the journal's cycle lock, canonical cycle replay, and existing live-row predicates; a busy, malformed, unreadable, active, or released-identity-blocked cycle remains untouched.
- Keep `pipeline-jobs/`, `reconcile-inventory/`, `.locks/`, and the scheduler state index outside the deletion scope. In particular, preserve #1775's rule that future state-index retention must retain an at-or-before-cutoff usable history anchor per `(model_id, source_id)`.
- Add fail-closed configuration, dry-run receipts, a disabled-by-default systemd timer, recovery instructions, and node-22 verification evidence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `pipeline-job-persistence`: add the retention and recoverability contract for cold-archiving expired file-journal cycles without deleting live scheduler authority.

## Impact

The change affects a new node-22 operational script, scheduler-journal tests, example environment settings, user systemd units, the production operations runbook, and the file-journal persistence specification. It adds no database, public HTTP API, state-index mutation, scheduler-pass behavior, or external Python dependency. Production enforcement remains disabled until a separate operator activation step; fixture design is required because this change deliberately removes files once enabled.
