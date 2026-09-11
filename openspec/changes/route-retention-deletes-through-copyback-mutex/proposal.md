## Why

Issue #2238. `services/orchestrator/retention.py` deletes run trees under the
**shared** object-store copyback root and takes no part in the cross-process
copyback mutex that #2035 (PR #2201) established on that same root.

`grep -c copyback_batch_lock services/orchestrator/retention.py` is `0`; the
three mentions of copyback in that file (`:23`, `:489`, `:670`) are comments.
Both callers pass `NHMS_OBJECT_STORE_COPYBACK_ROOT` in as an additional
runs-only root — `services/orchestrator/cli.py:196-199` and
`services/orchestrator/scheduler_runtime.py:2106-2109` both forward
the pair `WORKSPACE_ROOT` then the copyback root — and `_collect_run_targets`
(`retention.py:373-421`) enumerates exactly the per-run directories one level
under each swept root's `runs/`, which is the same path family
`run_tree_copyback._run_key` (`run_tree_copyback.py:263-270`) promotes into. Retention is the only destructive operator in the scheduler pass
and the only one outside the protocol.

This is not latent, and the premise was re-measured on node-22 for this change
rather than taken from the issue (2026-09-11, read-only, through the pinned
`/scratch/frd_muziyao/NWM/.venv/bin/python`; see `design.md` D1). The scheduler
reads an untracked `infra/env/compute.scheduler-dbfree.env` carrying
`NHMS_RETENTION_ENABLED=true`, `NHMS_RETENTION_DRY_RUN=false`,
`NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true` and
`NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store`, and
`nhms-compute-scheduler.timer` is `enabled` + `active`. Across the 450 pass
receipts now present, every one of the 413 carrying a retention block reports
`dry_run=False` and `extra_roots.enabled=True`, and 294 `deleted[]` entries —
all of them `runs/`-prefixed — landed on that copyback root, in six passes
between 2026-09-08T00:04:45Z and 2026-09-10T12:05:57Z. Those `rmtree`s ran with
no mutual exclusion at all on the NFS export node-22 and node-27 share.

#2035's impact sweep originally filed `retention.py` under "Unchanged downstream
consumers", reasoning that it never enumerates root-level files and so cannot
delete the lock file. That reasoning is true but answers the wrong question —
"can it destroy the lock file?", not "can it destroy a tree another writer is
promoting?" — and the misfiling is why this deletion lane was never pulled into
the protocol. #2035 corrected itself before this change began: at base
`6fdb2015`,
`openspec/changes/harden-copyback-batch-mutex-and-dir-traversal/design.md:535-546`
reclassifies `retention.py` as an "Unchanged **writer** that this change does not
bring into the protocol", states in so many words that listing it as a consumer
"was wrong and is the direct cause of it being missed", and routes it here. So
the documentation correction issue #2238 asks for is already satisfied upstream;
this change owes the code, not that edit.

## What Changes

- `run_retention` gains a keyword-only `copyback_root` parameter. Both callers
  pass the same value they already pass inside `runs_only_roots`, so the root
  that needs the mutex is named instead of being inferred from an untagged
  positional tuple.
- `_delete_entry` acquires `packages.common.copyback_guard.copyback_batch_lock`
  **per tree**, and only for entries whose root is the resolved copyback root.
  The `WORKSPACE_ROOT` lane and the primary object-store root keep their current
  unlocked deletion path.
- `CopybackLockError` (and therefore `CopybackLockTimeout`) joins
  `_delete_entry`'s `except` tuple. Both are `RuntimeError`, not `OSError`
  (`packages/common/copyback_guard.py:74,78`) — the same trap the existing
  `SafeFilesystemError` clause already documents. A lock failure becomes one
  `result.failed` entry and the pass continues.
- One pass-level lock-wait budget bounds the total time a sweep can spend
  blocked. Each acquisition is given whatever is left of it; once it is spent,
  the remaining copyback entries are recorded as failures without attempting to
  acquire. Without it a stuck holder — the NFS third state
  `copyback_guard.py:212-219` documents, correctly owned with no local holder
  and still locked — turns one 12-hourly pass into up to N × 900 s, and the
  measured N is already 48-54.
- The new suite is wired into `scripts/select_ci_tests.py` so a change to
  `services/orchestrator/retention.py`, `cli.py` or `__init__.py` selects it in
  the targeted PR lane. Without that wiring the CI gate's own directory-rule
  audit reds — correctly: an oracle no path rule routes to is invisible to
  exactly the PRs that need it. The broad orchestrator rule's frozen size pin in
  `tests/test_select_ci_tests.py` grows by one, which is that pin's own
  documented update protocol ("Growing the rule means consciously editing this
  list and recording the new lane wall-clock").
- `copyback_guard.py:54-67`'s budget comment names itself the single in-code home
  of the acquisition-count claim behind the 900 s default, and derives it from
  "at most once per cycle per scheduler pass". This change adds a per-tree
  acquirer, so that comment is updated to carry the new count and the retention
  budget that bounds it. It is a comment-only edit; no guard behaviour changes.
- No behavioral change on `dry_run`: `run_retention` returns before the deletion
  loop (`retention.py:839-840`), so zero acquisitions happen by construction.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `object-store-copyback-mutual-exclusion`: adds the deleter side of the
  protocol. The capability currently binds only writers that promote trees; this
  change adds the obligation that a process removing a directory tree under the
  copyback root holds the same mutex for that removal, and states precisely what
  the mutex does and does not guarantee against a retention pass.

  The delta is authored as `ADDED Requirements` under that capability's spec:
  `openspec/specs/object-store-copyback-mutual-exclusion/` does not exist yet —
  the capability lives only as a delta in the still-unarchived
  `harden-copyback-batch-mutex-and-dir-traversal` change — and openspec refuses
  `MODIFIED` against a non-existent target spec. `ADDED` applies correctly in
  either archive order and the two requirement names do not collide.

### Removed Capabilities

None.

## Non-Goals

- Retention policy. Which runs are selected, the cutoff, the frontier bound and
  the published-artifact protection are untouched; this change is only about
  whether the removal happens under the mutex.
- The `WORKSPACE_ROOT` lane. It is not a shared root and has no second writer;
  locking it would create a lock file for no mutual-exclusion benefit.
- The primary object-store root's `shutil.rmtree`, which stays as #1615/D6 left
  it.
- Closing the plan-to-delete window. A tree that a writer promotes and commits
  *before* retention acquires the mutex is still deleted by that pass if it was
  planned; see `design.md` D4 and the spec's explicit non-guarantee. This is
  worth stating plainly because the shape issue #2238's title reaches for —
  writer reports `ok`, tree gone — is on *this* side of the line, not the side
  the mutex closes. What the mutex closes is the pair of mid-promote
  interleavings D4 names, one of which destroys a writer's rollback material.
- `scripts/node27_raw_retention.py`, the second unlocked deleter on the same NFS
  export (`:553`). Registered in #2238's affected surface, tracked separately.
- #2236 and #2237, the two writer-side defects from the same review.
- Rolling back node-22's `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true`.
