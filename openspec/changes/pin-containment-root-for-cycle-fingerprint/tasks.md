# Tasks

Fixture level: `expanded`. Risk packs: filesystem containment (#1567: a
cheaper stat must refuse exactly what `stat_no_follow` refuses today);
journal cache correctness (a fingerprint that changes value, or stops
faulting, serves stale or tampered rows); handle lifetime (a leaked or
reused descriptor in a process that runs hundreds of passes a day);
concurrency (orchestration threads share one repository).

Must preserve: the result or the exception class (and `kind`) of
`stat_no_follow`, inside a block as outside, for every tree state that does
not change between the entry of the block and the call (the changes that do
differ are the rows of design.md's race table);
`stat_no_follow` outside a block, with `containment_root=None`, or with a
root other than the pinned one, runs exactly today's code; every other
`safe_fs` primitive is untouched; the value of
`_cycle_rows_source_fingerprint` and its fault marker for every tree; the
number of `_cycle_rows_source_fingerprint`, `_containment_stat_signature`
and module-level `stat_no_follow` calls per `_cycle_rows` call, and their
signatures; `_cycle_rows`, the caches, the write-window rules and
`_cycle_directories_probe_faulted` are not edited; `packages/common/safe_fs.py` stays at or under 1,000 lines (it has 956),
and `_open_directory_no_follow` and its callers stay in that file; existing
tests stay green unedited, in particular `tests/test_file_orchestration_journal_read_cache.py`,
`tests/test_gateway_reconcile_inventory.py`,
`tests/test_gateway_reconcile_round10.py`, `tests/test_safe_fs.py`.

## 1. Primitive (`packages/common/safe_fs.py`)

- [ ] 1.1 `pinned_containment_root(containment_root)` is a context manager.
  A module-level `_PINNED_CONTAINMENT_ROOT: ContextVar[... | None]` (default
  `None`) holds a small holder with the expanded root and the handle. On
  entry the root is expanded and opened with `_open_directory_no_follow`. If
  anything in that fails (`_expand_path` included: an unknown `~user`, a
  relative root with no current directory; `FileNotFoundError`;
  `SafeFilesystemError`; any other `Exception`), entry does not raise:
  nothing is pinned for this block. On exit (normal or by exception), in
  `finally`: the holder's handle is cleared, the context variable is reset
  with its token, and the handle, if any, is closed. A block that reuses
  an outer pin (1.4) neither clears nor closes the holder.
- [ ] 1.2 `_open_parent_dir` obtains its root handle from one helper. With a
  pin active for the same expanded root and the caller allowing it, the
  helper returns `os.dup` of the pinned handle (an `OSError` from `os.dup`
  is raised as `SafeFilesystemError` with `kind="io"`, as a failed chain
  open is today); otherwise it returns `_open_directory_no_follow(root)` as
  today. A holder whose handle was cleared counts as no pin. The rest of `_open_parent_dir`
  is unchanged, including the containment check ahead of it and the closing
  of the handle it was given. The pinned handle itself is never closed or
  returned by the walk.
- [ ] 1.3 Only `stat_no_follow` allows the pin, and only when its
  `containment_root` is not `None`. Its body after `_open_parent_dir` is
  unchanged (both `_verify_fd_matches_path` calls, the leaf stat without
  following, the symlink-leaf refusal, the error mapping). No other public
  function changes behaviour inside a block.
- [ ] 1.4 Nesting: a block for the root already pinned reuses that handle
  and does not close it on its own exit; a block for another root pins its
  own and restores the outer pin on exit. There is one pin at a time: inside
  the inner block a call that names the outer root takes today's path. The name is importable from
  `packages.common.safe_fs`.

## 2. Fingerprint (`services/orchestrator/file_orchestration_journal.py`)

- [ ] 2.1 The whole body of `_cycle_rows_source_fingerprint` runs inside
  `with pinned_containment_root(self.root):`. The name is imported into the
  journal module's namespace like the other `safe_fs` names, and looked up
  there at call time. No other line of the journal changes.

## 3. Tests

New file `tests/test_safe_fs_pinned_root.py`:

- [ ] 3.1 Outcome matrix. For each row, build the tree in `tmp_path`, call
  `stat_no_follow(path, containment_root=root)` outside a block and inside
  one, and assert the same outcome: for a result, equal `(st_mode, st_ino,
  st_dev, st_size, st_mtime_ns)`; for an exception, the same class and, for
  `SafeFilesystemError`, the same `kind`. The block is entered first and
  `pytest.raises` wraps only the call, so an exception escaping from entry
  fails the test. Rows: regular file; directory; file directly under the
  root (parent is the root), stat'ed twice in one block; absent leaf; absent
  component under the root; absent root; symlink leaf; symlinked component
  under the root; non-directory component under the root; root is a symlink
  to a real directory; a component above the root is a symlink; path outside
  the root; `root / "sub" / ".."` where `sub` is a real directory (the one
  `..` shape that only the under-root check refuses).
- [ ] 3.2 The root chain is opened once per block. Count calls of
  `_open_directory_no_follow` (replace the module attribute with a counting
  wrapper): five `stat_no_follow` calls for five paths under the root give
  five outside a block and one inside. A call inside the block with
  `containment_root=None`, and one with a different root, each add one
  (today's path).
- [ ] 3.3 Other primitives ignore the pin: inside a block,
  `read_bytes_no_follow(path, containment_root=root)` and
  `open_file_no_follow(path, containment_root=root)` each open the root
  chain themselves (the counter rises).
- [ ] 3.4 Handle lifetime, one test function per assertion so that a
  mutation log shows which one failed. Record the pinned handle number inside the block
  (through `_PINNED_CONTAINMENT_ROOT`); after the block `os.fstat` on it
  raises `OSError` (`EBADF`) and the context variable is back to its
  previous value. Same when the body raises. When entry cannot open the
  root, the context variable holds no pin inside the block. A context copied
  inside the block (`contextvars.copy_context()`) and run after it sees no
  usable pin: a stat run in it opens the chain itself. Leak oracle: after 200
  blocks of five stats each, no open descriptor of the process refers to a
  directory of the test tree (compare `os.fstat(fd)` `(st_dev, st_ino)` of
  every entry of `/dev/fd` with the tree's directories), which is immune to
  unrelated descriptors opening or closing.
- [ ] 3.5 Races, with the change made between two calls of one block. Each
  case also records what the same second call does outside a block, so the
  test documents both columns of the design's race table:
  (a) a directory under the root is replaced by a symlink to a copy: the
  next call raises `SafeFilesystemError`, as outside;
  (b) the root is renamed away and a different directory with the same
  child names is created at its path: the next call raises
  `SafeFilesystemError` (outside a block it returns the other directory's
  entry);
  (c) a directory above the root is renamed and a symlink to it put at its
  path: in the same block both `root/a/f` and `root/g` still return the real
  entries' stats (the stated limit), and the first call of a new block
  raises `SafeFilesystemError`;
  (c2) the root itself is renamed and a symlink to it put at its path: in
  the same block `root/a/f` returns the real stat and `root/g` (parent is
  the root) raises `SafeFilesystemError`;
  (d) the root is renamed away and nothing is put back: the next call raises
  `SafeFilesystemError` with `kind="io"` (outside a block:
  `FileNotFoundError`).
  The docstrings of (c) and (c2) say they pin stated limits and why the
  returned values are the real files'.
- [ ] 3.6 Nesting and threads. Same-root nesting: one chain open in total,
  and the outer block still works after the inner exits. Different-root
  nesting (outer `A`, inner `B`): stats under `B` inside the inner block use
  `B`'s pin (chain opens so far: two); one stat under `A` inside the inner
  block returns the right entry and opens the chain itself (three); after
  the inner block exits a stat under `A` opens nothing more (still three).
  A second thread started inside a block sees no pin (its stat opens the
  chain itself).

New file `tests/test_cycle_rows_fingerprint_pinned_root.py`:

- [ ] 3.7 Value equality. For each tree, compute
  `repository._cycle_rows_source_fingerprint(...)` normally and with
  `pinned_containment_root` in the journal module replaced by a context
  manager that pins nothing, and assert equality. Trees: empty journal root;
  a cycle with latest views, journal, events and by-cycle direct jobs under
  one source spelling; the same under both spellings; the journal root
  directory not existing at all (the repository constructor does not create
  it); a cycle with rollover
  segments (`.1`, `.2`); source directory absent on one surface;
  `journal/<source>` replaced by a symlink to a copy; a segment slot that is
  a symlink; `latest/<source>/<cycle>` a symlink; `pipeline-jobs` a symlink;
  the journal root itself reached through a symlink. For the symlink trees
  both sides are `_FINGERPRINT_CONTAINMENT_FAULT` (identity), for the others
  both are equal tuples that are not the marker. The tests call the
  fingerprint with `source_segments` passed explicitly (`("gfs",)` or
  `("gfs", "GFS")`), because `_cycle_read_source_segments` folds the two
  spellings into one on a case-insensitive filesystem; the trees that need
  two distinct directories are skipped there and run on node-27.
- [ ] 3.8 Call shape. For a populated cycle and `source_segments=("gfs",
  "GFS")` passed explicitly, one fingerprint
  calls `repository._containment_stat_signature` 21 times, each with exactly
  one positional argument and no keyword; calls the journal module's
  `stat_no_follow` 21 times with `containment_root=repository.root`; and
  opens the root chain once (count `_open_directory_no_follow` in
  `packages.common.safe_fs`). With the block replaced by the no-pin stand-in
  the chain is opened 21 times. The test derives 21 from the slot list
  (`_journal_segment_names`) and the number of spellings, not from a
  literal.
- [ ] 3.9 Through the cache. On a populated cycle: a first
  `_cycle_rows`-backed read stores; a second is a hit; then
  `journal/<source>` is replaced by a symlink to a copy and the next read
  raises what a cold repository raises on that tree (same class and token);
  then the tree is restored and a read stores again. This is the issue's
  "symlink-swap case that fails loud as on a cold read" for the new path.

## 4. Evidence

- [ ] 4.1 Red first. The new test files cannot run against the unmodified
  tree (the name they import does not exist); record that import failure in
  `.workplans/3236/red-runs-b1.log`. Then, with `pinned_containment_root`
  implemented as a block that pins nothing and the fingerprint already
  entering it, record that the "opened once" assertions of 3.2 and 3.8 fail
  (5 and 21 chain opens) while 3.1 and 3.7 pass.
- [ ] 4.2 Mutations, each applied by hand, the named test run, the outcome
  recorded in the same log, the file restored and compared with a scratch
  copy (`cmp`):
  - M1 the pinned path skips the under-root containment check -> 3.1
    (`root / "sub" / ".."`) red; the outside-the-root row stays green
    under M1 because `_anchor_for` checks the parent again, and the log says
    so;
  - M2 the pin is honoured for any root -> 3.2 (different root) and 3.6 red;
  - M3 the walk closes the pinned handle instead of a duplicate -> 3.1 (file
    directly under the root, second stat) red;
  - M4 exit does not close the handle -> 3.4 red;
  - M5 exit does not reset the context variable -> 3.4 (the context
    variable is back to its previous value) red; a stat after the block
    stays green, because the cleared holder counts as no pin, and the log
    says so;
  - M6 entry propagates the root's `FileNotFoundError` -> 3.1 (absent root)
    and 3.7 (journal root not existing) red;
  - M7 every primitive honours the pin -> 3.3 red;
  - M8 the fingerprint does not enter the block -> 3.8 red;
  - M9 the pin is stored on the module (a global) instead of a context
    variable -> 3.6 (thread) red;
  - M10 with a pin, `stat_no_follow` skips `_verify_fd_matches_path` -> 3.5
    (b) red (it returns the old directory's entry);
  - M11 exit closes the handle without clearing the holder -> 3.4 (copied
    context) red.
- [ ] 4.3 Micro-benchmark, recorded, not asserted: on a synthetic
  two-spelling cycle under a root six directories deep, wall time of 2,000
  fingerprints with the block and with the no-pin stand-in, on the Mac and
  on node-27. Written to `.workplans/3236/bench-b1.log`.
- [ ] 4.4 Local: the two new files; `tests/test_safe_fs.py`,
  `tests/test_safe_fs_lock.py`,
  `tests/test_file_orchestration_journal_read_cache.py`,
  `tests/test_gateway_reconcile_inventory.py`,
  `tests/test_gateway_reconcile_round10.py`,
  `tests/test_file_journal_cluster_map.py`,
  `tests/test_file_journal_late_lookup_seams.py`,
  `tests/test_file_journal_mixin_seams.py`,
  `tests/test_file_orchestration_journal.py`; `uv run --no-sync ruff check
  --no-cache .`; `openspec validate
  pin-containment-root-for-cycle-fingerprint --strict --no-interactive`; the
  repository entropy hard gate stays at its baseline; `wc -l
  packages/common/safe_fs.py` is at most 1,000.
- [ ] 4.5 node-27 (Linux `O_NOFOLLOW` semantics are the production ones):
  the same files in a temporary detached worktree, without the integration
  opt-in, so no database is created or dropped. Receipt on the PR.
- [ ] 4.6 After merge, before the production checkout moves: one plan-only
  pass of the merged commit from a separate clone at a pass boundary, with
  the sampling profiler. Record status, counts and the per-source backfill
  audit against the neighbouring production passes (must be identical), wall
  and CPU, and the samples under `_cycle_rows_source_fingerprint` against
  the prediction (about 700, from 2,972). Posted on #3236. Deployment itself
  follows the compute-node cadence.
