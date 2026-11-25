# Design

## Context

`stat_no_follow(path, containment_root=R)` today:

1. checks that `path` stays under `R` (`_relative_parts_under_root`);
2. opens `R` through `_open_directory_no_follow(R)`: `/` is opened and
   compared with its `lstat`, then every component of `R`'s absolute path is
   opened with `O_NOFOLLOW | O_DIRECTORY`;
3. opens every component between `R` and the parent of `path` the same way;
4. compares the parent handle with `lstat(parent)`, stats the leaf relative
   to the handle without following, refuses a symlink leaf, compares again;
5. closes the handles.

Step 2 costs one `openat` per component of the root's absolute path and is
repeated for each of the 21 paths of one fingerprint.

## Shape

```text
with pinned_containment_root(R):          # opens step 2 once, or pins nothing
    stat_no_follow(p1, containment_root=R)  # steps 1, 3, 4 from a dup of the pin
    stat_no_follow(p2, containment_root=R)
# handle closed, context variable reset
```

`_open_parent_dir` is the only function that asks for a root handle. It
gets one from a helper that returns a duplicate of the pinned handle when a
pin for the same expanded root is active, and a fresh `_open_directory_no_follow`
otherwise. From there on the function is unchanged, including that it closes
the root handle it was given (a duplicate, in the pinned case, never the pin
itself).

Only `stat_no_follow` passes "a pin may be used"; every other caller of
`_open_parent_dir` passes nothing and gets today's behaviour.

## Outcome table (identical with and without a block, for a tree that does not change after the block is entered)

| Tree state at the time of the call | Outcome |
|---|---|
| regular file, directory | `os.stat_result` of the entry |
| leaf absent, parents real | `FileNotFoundError` |
| a component under the root absent | `FileNotFoundError` |
| root absent | `FileNotFoundError` |
| leaf is a symlink | `SafeFilesystemError` |
| a component under the root is a symlink | `SafeFilesystemError` |
| a component under the root is not a directory | `SafeFilesystemError` |
| root is a symlink, or a component above it is | `SafeFilesystemError` |
| path not under the root, or with `..` | `SafeFilesystemError` |

For the last three rows the block pins nothing (D3) or the containment check
runs before any handle is used, so the call takes today's code.

## Races

A change made after the block was entered and before a call of that block:

| Change during one block | Today (per call) | In a block |
|---|---|---|
| component under the root -> symlink | refused | refused (opened per call) |
| root -> another directory that has the entry | describes the other directory | refused: handle and path disagree |
| root -> another directory, entry only there | describes it | `FileNotFoundError` when a directory between the root and the parent exists only in the new root; otherwise refused by the identity check |
| root renamed away, nothing at its path | `FileNotFoundError` | `SafeFilesystemError`, `kind="io"` (the parent's path cannot be stat'ed) |
| root -> symlink to the same directory, parent of the path is the root | refused | refused (the identity check finds a symlink) |
| root -> symlink to the same directory, parent deeper than the root | refused | **not seen**; the entry's real stat is returned |
| component above the root -> symlink to the same directory | refused | **not seen** by this block; the next block pins nothing and every call is refused |

For the fingerprint, whose last path (`pipeline-jobs`) has the root as its
parent, rows one to six end in the fault marker, and only the last row is a
fingerprint computed across an unseen swap. It still describes the real
files.

## Handle lifetime

- Opened on entry, closed in `finally` on exit, including when the body
  raises and when entry itself fails after the open.
- The context variable is reset with its token, so a nested block restores
  the outer pin and an exited block leaves no reference to a closed handle.
- The context variable holds a small holder object, not the bare handle.
  Exit clears the holder's handle before closing it, so a context that was
  copied while the block was open (an explicit `copy_context`, or a runtime
  that lets new threads inherit it) finds no pin afterwards and takes
  today's path instead of a closed descriptor.
- A `ContextVar` is per thread (and per task); a scope opened by one thread
  is invisible to threads that do not copy its context, which keeps the journal's "caches are safe
  under concurrent orchestration threads" requirement trivially true: there
  is no shared state.

## Alternatives rejected

- **A new parameter** (`anchor=`) on `_containment_stat_signature` /
  `stat_no_follow`: breaks the three existing tests that replace those names
  with functions of today's signature.
- **A batch API** (`stat_many_no_follow(paths)`): the fingerprint would stop
  calling `_containment_stat_signature` per path, same breakage, and a second
  copy of the walk.
- **Caching parent handles too** (`journal/<src>` is the parent of four
  slots): saves a further two to three `openat` per path, but then a swap
  under the root is no longer seen per call. Not needed for the predicted
  gain.
- **Fingerprint once per cycle per scoring scope**: changes the revalidation
  rule; out of scope by decision.
