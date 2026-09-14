# node-27 live raw-retention pass — 2026-09-14 (EF-18)

- Host: node-27 `ghdc`, account `nwm` (uid 1005) — the production unit's account.
- Code: isolated worktree `/home/nwm/NWM-wt-2361` detached at
  `75556204f61a978afd9c951317090396219dc4f1`.
- Invocation: through `scripts/node27_raw_retention_once.sh` with
  `NODE27_RAW_RETENTION_REPO` pointing at the worktree, the production env file
  copied in at mode 600 (removed afterwards), a private log/summary root under
  `/home/nwm/tmp/ef18-2361`, and the default wrapper lock path
  `/tmp/node27-raw-retention.lock` shared with the timer (next timer tick was
  11 h away). The database URL is not reproduced here.

## Lock file, before and after (`stat -c '%i %a %u %s %Y'`)

```
before: 12453911 600 1103 0 1789283759
after:  12453911 600 1103 0 1789283759
```

Same inode, mode, owner, size and mtime: the pass neither created, replaced nor
modified the lock file.

## Result

- Wrapper exit code: `1`.
- Summary: `schema_version nhms.node27_raw_retention.production.v5`,
  `status completed`, `execution_mode production_execute`, cutoff
  `2026-08-30T12:00:00Z`.
- `counts`: planned 4, deleted 2, failed 2, skipped 123; `freed_bytes`
  101797852.
- Planned by lane: raw 2, canonical 2. Deleted by lane: raw 2.
- `copyback_lock_failures`: `lock_unsafe 2`, `lock_timeout 0`,
  `lock_budget_exhausted 0`.
- `failed[]`:
  - `canonical/gfs/2026083000` — `error_type CopybackLockError`,
    `lock_failure lock_unsafe`, `cannot acquire copyback batch lock
    /home/ghdc/nwm/object-store/.nhms-copyback-batch.lock: [Errno 13] Permission
    denied`.
  - `canonical/IFS/2026083000` — same.

This is the D5 state the owner accepted: the retention account is not the
copyback root owner, so canonical removals fail closed with `lock_unsafe`, raw
removals proceed, and the tick is red (rc 1). Pruning resumes when the unit runs
as uid 1103 (#2360). The `lockf` acquire-and-exclude path on the real lock file
is therefore not exercised live; cross-host exclusion rests on
`lock-interop-20260914.md`.
