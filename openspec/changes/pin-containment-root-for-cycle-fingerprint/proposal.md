# Pin the containment root for one cycle-rows fingerprint (#3236, stage B1)

## Why

After stage A (#3375) an idle production pass takes about 120 s. A sampling
profile of that code on production data (12,027 main-thread samples, 10 ms
each) puts 2,972 samples (24.7 %) in `_cycle_rows_source_fingerprint`.

The fingerprint is computed on every `_cycle_rows` call outside a write
window: three calls per model per cycle (`candidate_state`, and
`completed_pipeline_init_state_identity` from two loops), 132 models, 8
cycles, so about 3,170 fingerprints per pass. One fingerprint stats 21 paths
(10 per source-segment spelling, plus the `pipeline-jobs` root), each through
`stat_no_follow(path, containment_root=self.root)`.

Every one of those calls re-opens the containment root from `/`, one
`openat(O_NOFOLLOW)` per component of the root's absolute path, before it
walks the two to four components under the root. That root-chain open is
2,403 of the fingerprint's 2,972 samples (81 %). It is the same directory
every time.

## What changes

- `packages/common/safe_fs.py` gains a context manager,
  `pinned_containment_root(root)`. Inside it, `stat_no_follow(path,
  containment_root=root)` walks from one handle on the root that was opened,
  with today's no-follow chain, when the block was entered, instead of
  opening the chain again. Everything else in `stat_no_follow` is the code
  that runs today: the containment check on the path, the per-component
  no-follow opens under the root, the two path/handle identity checks, the
  refusal of a symlink leaf, the error mapping.
- `_cycle_rows_source_fingerprint` computes inside one such block on the
  journal root. Nothing else enters a block.

## What does not change, and why this shape

- **Every non-owner read still computes its fingerprint**, and a hit still
  needs fingerprint equality (#1567). No cache is added and none skips the
  fingerprint (#1661). The number of fingerprints, of
  `_containment_stat_signature` calls and of `stat_no_follow` calls per read
  is what it is today; only the work inside each `stat_no_follow` shrinks.
- **The fingerprint's value and its fault marker are unchanged** for every
  tree. The tests compare the fingerprint with and without the block over a
  matrix of trees.
- **The seams tests replace stay where they are.** One existing test replaces
  `repository._containment_stat_signature` with a one-argument function, two
  replace `stat_no_follow` in the journal module's namespace. The fingerprint
  keeps calling both names once per path with today's signatures, which is
  why the handle travels in a context variable and not in a new parameter.
- The issue's option 1 ("read and fingerprint the cycle once") would stop
  revalidating between the models of one cycle. This stage does not do that;
  it makes each revalidation cheaper. Sharing the decoded segment records
  between the models of a cycle, under the same per-read fingerprint, is the
  next stage and a separate change.

## Decisions

- **D1. Opt-in block, honoured by `stat_no_follow` only.** Other primitives
  (`read_bytes_*`, `open_*`, the scans, the writers) ignore it. A wider reach
  is not needed for the fingerprint and would widen what has to be proven.
- **D2. Bound to the call, not to the repository.** The handle lives in a
  `ContextVar`, set on entry and reset in `finally`; it is closed on every
  exit path. Another thread never sees it. Nothing survives from one
  fingerprint to the next, so each fingerprint proves the root chain itself.
- **D3. Entering never raises for the root.** If the root chain cannot be
  opened (absent, a symlink, a non-directory component), nothing is pinned
  and every `stat_no_follow` inside behaves as outside a block, raising what
  it raises today. Absence stays a raw `FileNotFoundError`, which the
  fingerprint maps to `None`, not to the fault marker.
- **D4. One implementation of the walk.** The pinned and the unpinned path
  differ only in where the root handle comes from (a duplicate of the pinned
  one, or a fresh open). The checks after that are shared code, so there is
  no second copy that can lose a check.
- **D5. The pin applies only to the same root.** A call with another
  `containment_root`, or with none, takes today's path. A block entered
  inside a block for the same root reuses the handle; for another root it
  pins its own and restores the outer one on exit.

## Known limits (stated, tested)

Today every `stat_no_follow` opens the root chain again, so each call judges
the chain as it is at that moment. Inside a block the chain is opened once,
and what a later call of the same block sees of a change to the chain is
decided by the two identity checks in `stat_no_follow`, which compare the
parent handle with what the parent's path names now:

- **A component above the root replaced by a symlink to the same
  directory** is not seen by the rest of the block. It is refused by the
  next fingerprint (the next `_cycle_rows` call). The fingerprint computed
  across the swap describes the real journal files, not a decoy.
- **The root itself replaced by a symlink to the same directory** is seen
  as soon as a path whose parent is the root is stat'ed (the identity check
  finds a symlink where the root was). The fingerprint's last path is
  `pipeline-jobs`, directly under the root, so the fingerprint computed
  across that swap is the fault marker, as today.
- **The root replaced by another directory, or renamed away**, changes what
  a later call of the block raises compared with today (the handle and the
  path disagree, or the path is gone): it refuses where today's call would
  have described the other directory, and it raises `SafeFilesystemError`
  with `kind="io"` where today's call would have raised `FileNotFoundError`.
  Both are stricter for the fingerprint: it becomes the fault marker.

Swaps of anything under the root (a surface directory, a source directory, a
leaf) are seen exactly as today, because those components are still opened
per call. Outcomes are identical to today's for every tree that does not
change between the entry of the block and the call.

## Expected effect

Predicted from the profile: the fingerprint drops from 2,972 samples to about
700 (the root chain is opened once per fingerprint instead of 21 times), so
an idle pass loses about 23 s of 120 s. The after-profile on the compute
control node tests this number. `journal_read_attribution` totals do not
change (no file read is added or removed).

Not addressed here, per the same profile: segment-existence probes on a miss
(969 samples), per-model decoding of the shared segments (808), per-model
validation of model-less records (957), latest-view reads (1,448), the
direct-job lane (665).

## Impact

- Code: `packages/common/safe_fs.py`, `services/orchestrator/file_orchestration_journal.py`
  (one `with` in one method).
- Specs: `safe-filesystem-primitive-contract` (+1 requirement),
  `pipeline-job-persistence` (+1 requirement).
- No schema, no evidence shape, no configuration, no migration.
