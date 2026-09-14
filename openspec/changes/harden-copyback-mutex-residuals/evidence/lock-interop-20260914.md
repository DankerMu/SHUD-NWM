# Cross-host lock interop receipt — 2026-09-14 (#2252)

Question #2252 requires answered before any code: does a lock taken on the NFS
server's local filesystem exclude a lock taken by an NFS client on the same
file, in both directions?

## Hosts

| Role | Host | Kernel | Path of the probe file | Account |
|---|---|---|---|---|
| NFS server | node-27 `ghdc` | 5.15.0-187-generic | `/home/ghdc/nwm/archive/.flock-probe-2252/probe.lock` on ext4 `/dev/mapper/ubuntu--vg-home` | `nwm` uid 1005 |
| NFS client | node-22 `xnode` | 6.8.0-124-generic | `/ghdc/data/nwm/archive/.flock-probe-2252/probe.lock` | `frd_muziyao` uid 1103 |

node-22 mount: `ghdc:/home/ghdc on /ghdc/data type nfs4
(rw,relatime,vers=4.2,...,hard,proto=tcp,sec=sys,local_lock=none,addr=10.0.1.27)`.
`local_lock=none` means the client sends both `flock` and POSIX locks to the
server as NFSv4 byte-range locks. The probe file was created `0666` in a
directory owned by `nwm`, so both accounts could open it `O_RDWR`; the directory
was removed after the run.

## Method

A stdlib-only probe opened the file `O_RDWR` and either held an exclusive lock
for 12 s or made one non-blocking attempt 5 s into the other host's hold.
Primitives: `flock` = `fcntl.flock(LOCK_EX)`; `lockf` = `fcntl.lockf(LOCK_EX)`
(POSIX record lock, whole file); `ofd` = `F_OFD_SETLK` with a whole-file
`F_WRLCK`. Interpreters: node-27 `/home/nwm/NWM/.venv/bin/python` (3.11.15),
node-22 `/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7).

## Result

| Holder | Attempt | Outcome |
|---|---|---|
| node-27 flock | node-22 flock | **ACQUIRED — no exclusion** |
| node-22 flock | node-27 flock | **ACQUIRED — no exclusion** |
| node-27 flock | node-27 flock | blocked (EAGAIN) |
| node-22 flock | node-22 flock | blocked (EAGAIN) |
| node-27 lockf | node-22 flock | blocked (EAGAIN) |
| node-22 flock | node-27 lockf | blocked (EAGAIN) |
| node-27 ofd | node-22 flock | blocked (EAGAIN) |
| node-22 flock | node-27 ofd | blocked (EAGAIN) |
| node-27 lockf | node-27 flock | **ACQUIRED — no exclusion** |
| node-27 ofd | node-27 flock | **ACQUIRED — no exclusion** |
| node-22 ofd | node-22 flock | blocked (EAGAIN) |
| node-22 flock | node-22 ofd | blocked (EAGAIN) |
| node-27 ofd | node-27 ofd | blocked (EAGAIN) |
| node-22 ofd | node-22 ofd | blocked (EAGAIN) |
| node-22 ofd | node-27 ofd | blocked (EAGAIN) |
| node-27 ofd | node-22 ofd | blocked (EAGAIN) |

## Conclusion

1. A node-27 `flock` and a node-22 `flock` on the same file do not exclude each
   other. Calling `acquire_copyback_batch_lock` unchanged on node-27 would
   produce a lock that serialises nothing against the node-22 writers.
2. A node-27 POSIX record lock (`lockf` or OFD) and a node-22 `flock` exclude
   each other in both directions: on the server both are POSIX locks in the
   same lock table; on local ext4 BSD `flock` and POSIX locks are independent.
3. The exclusion does not hold between two primitives *on node-27 itself*
   (`lockf`/`ofd` vs `flock` both acquire). Every current directory-tree
   copyback writer runs on node-22 (design.md D3 inventory), so this is a
   recorded constraint on future node-27 acquirers, not a live gap.

## Identity facts measured the same day

- `/home/ghdc/nwm/object-store` is owned by `frd_muziyao` (uid 1103), mode 775.
- `.nhms-copyback-batch.lock` exists, `-rw------- frd_muziyao`, size 0.
- The node-27 raw-retention user unit runs as `nwm` (uid 1005), which cannot
  open that file; `nwm` has no passwordless sudo.
- node-27 Python 3.11.15 `fcntl` has no `F_OFD_SETLK` attribute; node-22 3.12.7
  does (37). This is why the guard uses `lockf` (design.md D3).
