# Cross-host lock interop receipt: w2 / w3 / node-27, 2026-11-23

> Provenance: recorded during the 2026-11-23 compute migration (#3345) and checked in verbatim below this note.
> `lock-interop-20260914.md`, where referenced, is
> `openspec/changes/archive/2026-09-14-harden-copyback-mutex-residuals/evidence/` material; the decision it supports is
> `docs/adr/0008-cross-host-copyback-lock-primitive.md`.

Topology after the change: w3 serves /srv/nhms-scratch/frd_muziyao (ext4 LV, root-only backing path) to 10.0.1.12 and 10.0.1.13;
both nodes mount it at /scratch/frd_muziyao (w3 through a loopback NFS mount). w2 serves /srv/nhms-runs/runs the same way,
mounted on both at /scratch/frd_muziyao/nhms-prod/workspace/runs. node-27 serves /home/ghdc at /ghdc/data on both. All vers=4.2, local_lock=none.

## Before (w3 on local ext4, clients over NFS) - the hazard
kernel w3: 6.8.0-71-generic
| holder | attempt | outcome |
|---|---|---|
| w3-local flock | w3-loopback flock | ACQUIRED (no exclusion) |
| w3-loopback flock | w3-local flock | ACQUIRED (no exclusion) |
| w3-loopback flock | w3-loopback flock | BLOCKED |
| w3-local flock | w3-local flock | BLOCKED |
| w3-local lockf | w3-loopback flock | BLOCKED |

Job-side state-index lock is a blocking flock (packages/common/state_manager.py:4627 at 649f4ee8b) on
/scratch/frd_muziyao/nhms-prod/object-store/scheduler/state-index/, so local-vs-client access to that tree must not be mixed.

## After (every access is a client lock) - method: hold 9 s, one non-blocking attempt 3 s in
| tree | holder | attempt | outcome |
|---|---|---|---|
| w3 export (state index tree) | w3 flock | w2 flock | BLOCKED |
| w3 export (state index tree) | w2 flock | w3 flock | BLOCKED |
| w3 export (state index tree) | w3 flock | w3 flock | BLOCKED |
| w3 export (state index tree) | w2 flock | w2 flock | BLOCKED |
| w2 export (workspace/runs) | w3 flock | w2 flock | BLOCKED |
| w2 export (workspace/runs) | w2 flock | w3 flock | BLOCKED |
| node-27 export (copyback) | n27 lockf | w2 flock | BLOCKED |
| node-27 export (copyback) | w2 flock | n27 lockf | BLOCKED |
| node-27 export (copyback) | n27 ofd | w2 flock | BLOCKED |
| node-27 export (copyback) | w2 flock | n27 ofd | BLOCKED |
| node-27 export (copyback) | w2 flock | w3 flock | BLOCKED |
| node-27 export (copyback) | w3 flock | w2 flock | BLOCKED |
| node-27 export (copyback) | w2 flock | w2 flock | BLOCKED |

Probe inodes seen identically from both nodes: 13631494 (w3 export), 8786313 (w2 export), 13500605 (node-27 export).
Conclusion: flock excludes w2<->w3 on all three shared trees; ADR 0008 (node-27 lockf/OFD <-> client flock) holds for w2.
