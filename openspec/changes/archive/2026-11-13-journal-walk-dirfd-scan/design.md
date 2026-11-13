# Design — journal-walk-dirfd-scan

## D0. Scope: non-strict walks only

The fast path applies only when `_iter_discovered_files(..., strict_disappearance=False)` (the default; every hot
caller — `_iter_regular_json_files`, `_iter_jsonl_files`, the flat `pipeline-jobs/` listing, cycle replay signatures —
is non-strict). Walks with `strict_disappearance=True` (reconcile quiescence authority, inventory migration) keep
today's per-entry `stat_no_follow` path byte-for-byte: their lazy per-entry timing, per-entry fd identity checks and
every existing fault-injection test stay exactly as they are. This removes the snapshot-timing change (an entry
stat'd up front instead of after earlier subdirectories) from every authority decision.

## D1. Primitive contract

`packages/common/safe_fs.py`:

```python
@dataclass(frozen=True)
class ScannedEntry:
    name: str
    stat: os.stat_result | None      # lstat relative to the held dir fd; None when vanished or failed
    error: SafeFilesystemError | None  # per-entry non-FNF OSError, wrapped as SafeFilesystemError(kind="io")

def scan_directory_no_follow_limited(path, *, max_entries, containment_root=None) -> list[ScannedEntry]
```

- Opens `path` through the same descriptor chain `_list_directory_no_follow` uses, enumerates names with
  `os.scandir(fd)` bounded at `max_entries + 1` (sentinel; vanished names count), then for each name
  `os.stat(name, dir_fd=fd, follow_symlinks=False)`.
- Per-entry outcomes are RETURNED, never raised: FNF -> `stat=None, error=None` (vanished); other `OSError` ->
  `stat=None, error=SafeFilesystemError(..., kind="io")` (same wrapping `stat_no_follow` applies, so evidence
  `error_type` stays `"SafeFilesystemError"`); a symlink is returned with its `lstat` (the walk refuses it).
- Whole-directory failures (chain open, `scandir`) raise exactly as `list_directory_no_follow_limited` does today
  (FNF raw, anything else `SafeFilesystemError`), so the walk's existing listing `except` branch maps them unchanged.
- After the stats, one `_verify_fd_matches_path(fd, target)` (lstat, no `os.open`) restores the identity check that
  per-entry `stat_no_follow` performed; a mismatch raises `SafeFilesystemError` (whole-directory -> walk maps to
  `file_journal_unreadable`, fail closed); if the directory path itself no longer exists at that check, raise the raw
  `FileNotFoundError` instead, so the walk's listing FNF branch keeps today's outcome (non-strict: return/skip).
- The fd is closed on every path; the result is a materialized list (no fd held across a `yield`).

## D2. Non-strict walk mapping

In the non-strict branch the walk calls the primitive in place of BOTH `list_directory_no_follow_limited` and the
per-entry `stat_no_follow`. Then, iterating `sorted` by name, exactly in today's order of checks:
1. `_SAFE_SEGMENT_RE` on the name (the name was already lstat'd via `dir_fd`; that is harmless — no path is built or
   followed before this check);
2. vanished -> skip (non-strict branch of today's FNF handling);
3. `error` -> `file_journal_unsafe_scanned_entry`, field = entry, evidence `error_type = "SafeFilesystemError"`;
4. `S_ISLNK` -> `file_journal_unsafe_scanned_entry`, field = entry, evidence `error_type = "SafeFilesystemError"`
   (today's `stat_no_follow` raise), for any name/suffix and for links to directories;
5. directory -> recurse (when `recursive`), else skip; suffix match on a non-regular file -> `not_regular_file`.
The entry-limit check (`len(names) > remaining_entries` -> `file_journal_file_limit_exceeded`), `scanned_entries`
accounting, per-directory `stat_no_follow(current)`, depth limit and recursion through the path-based `walk` are
unchanged.

## D3. Seams

- The facade `services/orchestrator/file_orchestration_journal.py` imports `scan_directory_no_follow_limited` (and
  `ScannedEntry` if referenced) next to `stat_no_follow`/`list_directory_no_follow_limited` and lists it in `__all__`;
  `file_journal_paths.py` looks it up at call time as `file_orchestration_journal.scan_directory_no_follow_limited`
  (same pattern as the existing seams).
- Register the new patched/caller name in `tests/fixtures/file_journal_cluster_map.json` and update the
  `list_directory_no_follow_limited` callers entry so `tests/test_file_journal_cluster_map.py` stays green.
- Affected tests are found by grep, not by running suites (a neutralised negative guard stays green):
  `grep -rn 'journal_module, "stat_no_follow"\|journal_module, "list_directory_no_follow_limited"' tests/`. Today that
  is `tests/test_gateway_reconcile_round10.py`, `tests/test_gateway_reconcile_inventory.py`,
  `tests/test_gateway_reconcile_writer_quiescence.py`. Two classes:
  - positive injection (the fake must fire to produce the asserted outcome) on a NON-strict walk: wrap the new seam
    with the same injection, assert the fake was called, pin `.reason`;
  - negative guard (forbidden-directory / trap assertions that must never fire, e.g. inventory L200-226
    `bounded_list` / `bounded_stat` forbidding `pipeline-jobs`, `journal`, `pipeline-jobs/by-cycle`,
    `terminal_history`): apply the same forbidden-set and trap checks to the new seam and assert zero forbidden hits;
    do NOT require the fake to be called.
  Injections that reach only strict walks (round10, writer_quiescence) are untouched (D0).
- `tests/test_file_journal_late_lookup_seams.py:133` exercises a strict walk; its counts must stay unchanged.

## D4. Proof without timing

Count `os.open` calls (wrap `safe_fs.os.open`) while scanning a non-strict directory of N files under a root nested
D levels deep, for two values of N: the counts are EQUAL (independent of N). Do not hard-code a number (absolute depth
from `/` differs between macOS tmp and node-27). Also show the legacy strict path still scales with N (regression
guard that D0 did not leak).

## Risks

- NFS `ESTALE` on a held fd is mapped as a per-entry error (D1) -> `unsafe_scanned_entry` instead of a vanish skip;
  no concurrent deleter of scanned non-strict directories is known; fail closed.
- macOS vs Linux `dir_fd`: both support `stat(dir_fd=)` and `scandir(fd)`; node-27 is the oracle.
