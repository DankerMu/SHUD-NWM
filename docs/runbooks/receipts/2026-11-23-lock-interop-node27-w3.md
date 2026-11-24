# Cross-host lock interop receipt: node-27 <-> w3, 2026-11-23

> Provenance: recorded during the 2026-11-23 compute migration (#3345) and checked in verbatim below this note.
> `lock-interop-20260914.md`, where referenced, is
> `openspec/changes/archive/2026-09-14-harden-copyback-mutex-residuals/evidence/` material; the decision it supports is
> `docs/adr/0008-cross-host-copyback-lock-primitive.md`.

Server node-27 (10.0.1.11, kernel 5.15.0-198), file /home/ghdc/nwm/archive/.flock-probe-w3-20261123/probe.lock, account nwm (uid 1005).
Client w3 (10.0.1.13, kernel 6.8.0-71), same file at /ghdc/data/nwm/archive/..., account frd_muziyao (uid 1103). Same inode on both sides: 13500590.
Mount: 10.0.1.11:/home/ghdc on /ghdc/data type nfs4 (rw,relatime,vers=4.2,hard,proto=tcp,sec=sys,local_lock=none).
Method: same as lock-interop-20260914.md (hold 9 s, one non-blocking attempt 4 s in).

| Holder | Attempt | Outcome |
|---|---|---|
| node-27 lockf | w3 flock | blocked |
| w3 flock | node-27 lockf | blocked |
| node-27 ofd | w3 flock | blocked |
| w3 flock | node-27 ofd | blocked |
| w3 flock | w3 flock | blocked |
| node-27 flock | w3 flock | ACQUIRED (no exclusion, as in 2026-09-14) |
| w3 flock | node-27 flock | ACQUIRED (no exclusion, as in 2026-09-14) |
| node-27 lockf | node-27 lockf | blocked |

Conclusion: ADR 0008 holds for w3 as the NFS client.
