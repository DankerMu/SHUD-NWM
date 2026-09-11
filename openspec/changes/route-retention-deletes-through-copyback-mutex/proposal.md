## Why

Issue #2238. `services/orchestrator/retention.py` deletes run trees under the
**shared** object-store copyback root and takes no part in the cross-process
copyback mutex that #2035 (PR #2201) established on that same root.

Both callers pass `NHMS_OBJECT_STORE_COPYBACK_ROOT` in as an additional
runs-only root (`cli._run_cleanup`, `scheduler_runtime._run_retention`), and
`retention._collect_run_targets` enumerates exactly the per-run directories one
level under each swept root's `runs/` — the same path family
`run_tree_copyback._run_key` promotes into. Within the scheduler pass, retention
is the destructive operator that stands outside the protocol.

This is not latent. Re-measured on node-22 for this change on 2026-09-11,
read-only (design.md D1): the scheduler runs with `NHMS_RETENTION_ENABLED=true`,
`NHMS_RETENTION_DRY_RUN=false`, `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true` and
`NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store`, its timer is
enabled and active, and 294 `deleted[]` entries — all `runs/`-prefixed — landed
on that copyback root across six passes between 2026-09-08T00:04:45Z and
2026-09-10T12:05:57Z. Those `rmtree`s ran with no mutual exclusion at all on the
NFS export node-22 and node-27 share.

Retention is not the only unlocked deleter on that root:
`node27_raw_retention.run_retention` is a second one, on node-27, outside the
pass. The spec requirement names it as a known-violating implementation and
issue #2252 owns it.

#2035 corrected its own impact sweep before this change began — at base
`6fdb2015` its `design.md` reclassifies `retention.py` from "unchanged
downstream consumer" to an unchanged **writer** it does not bring into the
protocol, and routes the gap here. Issue #2238's documentation-correction
criterion is therefore already satisfied upstream; this change owes the code.

## What Changes

- `run_retention` gains a keyword-only `copyback_root` parameter. Both callers
  pass the same value they already pass inside `runs_only_roots`, so the root
  that needs the mutex is named instead of being inferred from an untagged
  positional tuple.
- `_delete_entry` takes the mutex **per tree**, and only for entries whose root
  is the resolved copyback root. It calls
  `copyback_guard.acquire_copyback_batch_lock` /
  `copyback_guard.release_copyback_batch_lock` directly rather than the
  `copyback_batch_lock` context manager, because the elapsed acquisition time
  has to be measured between those two calls to be charged against the pass
  budget (design.md D9); the release is in a `finally`. The `WORKSPACE_ROOT`
  lane and the primary object-store root keep their current unlocked deletion
  path.
- `CopybackLockError` (and therefore `CopybackLockTimeout`) joins
  `_delete_entry`'s `except` tuple. Both are `RuntimeError`, not `OSError` — the
  same trap the existing `SafeFilesystemError` clause already documents. A lock
  failure becomes one `result.failed` entry and the pass continues.
- One pass-level lock-wait budget bounds the total time a sweep can spend
  blocked. Each acquisition is given whatever is left of it; once it is spent,
  the remaining copyback entries are recorded as failures without attempting to
  acquire (design.md D9).
- The new suite is wired into `scripts/select_ci_tests.py` so a change to
  `services/orchestrator/retention.py`, `cli.py` or `__init__.py` selects it in
  the targeted PR lane, and the frozen broad-rule size pin in
  `tests/test_select_ci_tests.py` grows by one, which is that pin's own
  documented update protocol.
- `copyback_guard`'s budget comment names itself the single in-code home of the
  acquisition-count claim behind the 900 s default and derives it from "at most
  once per cycle per scheduler pass". This change adds a per-tree acquirer, so
  that comment is updated to carry the new count and the retention budget that
  bounds it. Comment-only; no guard behaviour changes.
- No behavioural change on `dry_run`: `run_retention` returns before the
  deletion loop, so zero acquisitions happen by construction.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `object-store-copyback-mutual-exclusion`: adds the deleter side of the
  protocol. The capability currently binds only writers that promote trees; this
  change adds the obligation that a process removing a directory tree under the
  copyback root holds the same mutex for that removal, and states precisely what
  the mutex does and does not guarantee against a retention pass.

  Authored as `ADDED Requirements`: the target spec directory does not exist yet
  (the capability lives only as a delta in the still-unarchived
  `harden-copyback-batch-mutex-and-dir-traversal` change) and openspec refuses
  `MODIFIED` against a non-existent target spec. `ADDED` applies correctly in
  either archive order and the two requirement names do not collide (design.md
  D7).

### Removed Capabilities

None.

## Non-Goals

- Retention policy. Which runs are selected, the cutoff, the frontier bound and
  the published-artifact protection are untouched; this change is only about
  whether the removal happens under the mutex.
- The `WORKSPACE_ROOT` lane. It is not a shared root and has no second writer.
- The primary object-store root's `shutil.rmtree`, which stays as #1615/D6 left
  it.
- Closing the plan-to-delete window. A tree that a writer promotes and commits
  *before* retention acquires the mutex is still deleted by that pass if it was
  planned; see design.md D4 and the spec's explicit non-guarantee. The shape
  issue #2238's title reaches for — writer reports `ok`, tree gone — is on
  *this* side of the line. What the mutex closes is the pair of mid-promote
  interleavings D4 names, one of which destroys a writer's rollback material.
- `scripts/node27_raw_retention.py`, the second unlocked deleter on the same NFS
  export. Registered in #2238's affected surface, tracked as #2252.
- #2236 and #2237, the two writer-side defects from the same review.
- Rolling back node-22's `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true`.
