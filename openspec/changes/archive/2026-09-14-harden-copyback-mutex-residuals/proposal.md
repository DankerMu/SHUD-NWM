## Why

One batch, one lock adjudication. Five open issues are residuals of the copyback
batch mutex that #2035 (PR #2201) and #2238 (PR #2257) established on the shared
object-store copyback root. Landing them separately would re-open the mutex
contract five times; this change settles it once.

- **#2237** — `run_tree_copyback._replace_tree` / `_replace_file` delete their
  backup in `finally` even when the restore failed or was never attempted, so a
  second-order failure on a re-copyback destroys the destination's only copy of
  `runs/<run_id>`. Under the #2035 mutex no competitor can occupy the target, so
  that destructive path is the only failure path left.
- **#2236** — `forcing_copyback_backfill._plan_or_apply_packages` decides
  `already_present` from a destination read taken outside the mutex, so an
  `--apply` report can count a package as present that a competitor's batch
  rollback removed a moment later. The mutual-exclusion spec records this as an
  accepted limit naming #2236.
- **#2252 + #2239** — `node27_raw_retention.run_retention` removes
  `canonical/<storage-source>/<cycle>` trees on node-27 with no lock, inside the
  key space `publisher._copyback_canonical_precip` and
  `scripts/canonical_precip_copyback_backfill.py` promote under the mutex on
  node-22. The spec names it as a known violator. Measured on 2026-09-14
  (design.md D3, `evidence/lock-interop-20260914.md`): node-27 is the NFS server
  and a node-27 `flock` does **not** exclude a node-22 NFS-client `flock` in
  either direction, so calling the existing guard there would be a lock that
  looks held and serialises nothing. A node-27 POSIX record lock does exclude
  it, in both directions.
- **#2262** — four deferred residuals of PR #2257: the explicit timeout argument
  accepts `inf`/`NaN`; retention's `failed[]` collapses three lock failure shapes
  into one untyped entry and compaction drops even that; the tolerated
  plan-to-delete window has no test; and the retention waiter count is
  unbounded. Its issue body was filed with #2261's text (no edit history); the
  scope here is the title's four items as routed from PR #2257's known limits.

## What Changes

- `copyback_guard`: the explicit timeout branch refuses non-finite values; a
  `posix` lock primitive (`fcntl.lockf`) is added beside the default `flock`,
  with the same identity checks, deadline and error types; a
  `CopybackLockBudgetExhausted` error and one failure-kind classifier shared by
  both retention lanes.
- `run_tree_copyback`: backup lifecycle keyed on explicit `promoted`/`restored`
  state; restore is attempted before temp cleanup and a cleanup failure cannot
  pre-empt it; an unrestored backup is kept and its path is reported in the
  raised `RunTreeCopybackError.details`. Both helpers.
- `forcing_copyback_backfill`: in `--apply`, one mutex acquisition per package
  covers the destination read, the skip decision, source validation, copy, and
  commit/rollback. Plan mode stays unlocked and says so per package
  (`observed_under_lock: false`).
- `node27_raw_retention`: the canonical lane removes each tree under the
  mutex, acquired with the `posix` primitive, per tree, with a pass-level wait
  budget; a lock failure is that entry's `failed[]` record with a typed
  `lock_failure`; dry-run, disabled and preflight-blocked runs acquire nothing.
  The raw and precip-cache lanes stay unlocked (design.md D4).
- `services/orchestrator/retention`: lock failures carry the same typed
  `lock_failure`, and a scalar `copyback_lock_failures` block survives
  `scheduler_evidence_payload._compact_retention`.
- Specs: the two mutual-exclusion requirements are rewritten (the #2236
  advisory paragraph and the #2252 known-violator clause are retired), a
  run-tree backup requirement is added, and the node-27 raw-retention
  requirement for canonical deletability is updated for the lock identity.
- ADR 0008 records the cross-host lock primitive decision.

## Impact

- **Behaviour change on node-27, accepted by the owner on 2026-09-14**: the
  lock file is `0600` owned by the copyback root owner (uid 1103) and the
  retention unit runs as `nwm` (uid 1005). The guard's identity contract is
  unchanged, so after deploy every aged canonical cycle fails closed with
  `lock_failure: lock_unsafe` and is not removed until the unit runs as the
  root owner — tracked by a follow-up ops issue. Raw and precip-cache pruning
  are unaffected. The canonical mirror measured 3.7 GiB for 14 days of both
  sources against 1.1 TiB free on `/home`.
- node-22 behaviour of the writers is unchanged (default primitive stays
  `flock`); the node-22 code changes (#2237, #2236, retention signal) deploy
  only in the #1831 maintenance window, and node-22's live `flock` writers
  already exclude the node-27 `posix` acquirer without any node-22 deploy.
- Report fields are additive (`observed_under_lock`, `lock_failure`,
  `copyback_lock_failures`); the backfill's `copyback_lock_unavailable`
  category is unchanged; the node-27 summary schema string moves to `v5`.
- `run_tree_copyback`: a retained-backup failure is now a `RunTreeCopybackError`
  instead of a bare `OSError`, so the forecast chain's copyback stage records it
  as its own failure event carrying the backup path.
- While the node-27 unit is not the copyback root owner, each tick with an aged
  canonical cycle exits 1 (`Result=failed`); the unit has no `OnFailure=`.
