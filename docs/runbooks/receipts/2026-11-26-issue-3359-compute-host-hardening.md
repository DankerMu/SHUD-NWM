# Compute host hardening receipt (issue #3359 items 1, 2 and 5; 2026-11-26)

Host-side changes on the compute role's two hosts, `w3` (control) and `w2`, made on 2026-11-26 after the owner decided
to do all six items of #3359. Each change was made with an empty Slurm queue and no scheduler pass running; none needed
the scheduler timer stopped. Item 6 (cold-boot drill) is not done.

## Item 1: the production user's user manager is masked on `w2` (13:50Z)

The production user's home is shared to `w2` over NFS, so a login there would start a second set of scheduler timers.
Before this change only discipline prevented it.

| Check | Result |
|---|---|
| before | `user@1103.service` `static` and `inactive`; no session of the production user, empty linger directory, no process of that uid |
| `systemctl mask user@1103.service` | `/etc/systemd/system/user@1103.service -> /dev/null` |
| `systemctl is-enabled user@1103.service` | `masked` |
| `systemctl start user@1103.service` | refused: `Unit user@1103.service is masked.` |
| a one-CPU Slurm job on `w2` after the mask (job `1003641`) | `COMPLETED 0:0`, running as the production user; afterwards still no session and no process |

Rollback: `systemctl unmask user@1103.service` on `w2`.

## Item 2: nothing is written under the runs mount point of `w3` while `w2` is away (13:51Z)

The runs directory of both hosts is `w2`'s export, mounted on a directory that lives on `w3`'s scratch. With `w2` down
that mount is skipped (`nofail`), and until this change `w3` accepted jobs and wrote results into the directory under
the mount point, where the later mount hid them.

Two changes on `w3`:

1. The directory under the mount point (backend path `/srv/nhms-scratch/frd_muziyao/nhms-prod/workspace/runs`, empty)
   went from the production user and mode `755` to `root:root` and mode `0000`. Unmounted, a write by the scheduler
   pass or by a job now fails with `Permission denied`.
2. `/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf` requires the runs mount as well, followed by
   `systemctl daemon-reload`.

Rehearsal on a sibling probe directory (`root:root`, `0000`; removed afterwards), because a remount over the real
directory happens only at boot:

| Step | Result |
|---|---|
| production user, `touch` and `ls` inside the probe through `/scratch/...` | `Permission denied` |
| root on `w3` mounts the runs export on the probe | mounted; the production user writes, reads and deletes a file through it; after `umount` the probe is `root:root 0000` and empty |
| the same on `w2` (the probe seen through `w3`'s scratch export) | mounted; write, read and delete as the production uid succeed |

The real change:

| Check | Result |
|---|---|
| directory under the mount point | `frd_muziyao:huser 755`, empty → `root:root 0`; the production user reads and writes through the existing mount as before |
| drop-in sha256 | before `506ce7a0324f2c05…` (backup on `w3` in `/var/backups/nhms-issue3359/`), after `ed509e4e5e90094f8612e89a2fde08e983cfaa443ca0da09414c28131f2c777b` |
| `findmnt` (nfs4, ext4) and the main PIDs of `slurmd` and `slurmctld`, before and after `daemon-reload` | identical |
| `systemctl show slurmd -p RequiresMountsFor` | `/scratch/frd_muziyao /scratch/frd_muziyao/nhms-prod/workspace/runs /ghdc/data /run/slurm` |
| `systemctl list-dependencies slurmd` | contains the mount unit of the runs path |
| one job on each node writes, reads and deletes a file under `workspace/runs` (jobs `1003642` on `w3`, `1003643` on `w2`) | both `COMPLETED 0:0`; the mount source on both is `w2`'s runs export |
| afterwards | both nodes `idle`, gateway health `healthy`, the 13:48Z stall-health probe `success` |

Rollback on `w3`: the backend directory back to `frd_muziyao:huser` and `755`; the drop-in restored from the backup;
`systemctl daemon-reload`.

Not proven: that `slurmd` on `w3` stays down while `w2` is off, and that nothing lands under the mount point then. Only
the cold-boot drill (item 6) can show it.

## Item 5: the system-level files as read on the hosts (14:05Z)

Source of `infra/systemd/compute-system/`. All files are `root:root`, mode `0644`.

| Host | Path | sha256 | mtime (UTC) |
|---|---|---|---|
| w3, w2 | `/etc/systemd/system/nhms-mounts-ensure.service` | `9d306a81118ea2b1693c780d78145c11cee346651a330cf4fbdf1d4d304e3c82` | 2026-11-23 10:35 |
| w3, w2 | `/etc/systemd/system/nhms-mounts-ensure.timer` | `fa442413fc36126b99ee8fed4eb76773c8b52ac1f9c7fed19efbdb419ca771ed` | 2026-11-23 10:35 |
| w3 | `/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf` | `ed509e4e5e90094f8612e89a2fde08e983cfaa443ca0da09414c28131f2c777b` | 2026-11-26 13:51 |
| w2 | `/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf` | `af2cd66ac13913091375a17f5c895b7237412b7fbf5cf1303a3c39f8f883b485` | 2026-11-23 10:39 |
| w2 | `/etc/tmpfiles.d/nwm-smt-off.conf` | `c78d7b2d2fa5aad18788be62b84065eecad0bfb5f1ff4f10cc0a4f063dc76fd4` | 2026-11-26 13:29 |

`/etc/tmpfiles.d/nwm-smt-off.conf` was installed the same day for #3384: it writes `off` to
`/sys/devices/system/cpu/smt/control` at boot. Its write was tested by switching SMT on and applying the file; that it
holds across a reboot is not proven.
