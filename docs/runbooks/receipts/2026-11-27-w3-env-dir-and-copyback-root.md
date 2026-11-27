# Receipt: env directory, unit env files and copyback root on w3, 2026-11-27

> Provenance: read-only commands run on `w3` (the compute control host) for #3417, which rewrote
> `infra/env/README.md`. Several rules of that file rest on observations made on the retired node-22 host
> on 2026-09-02 and 2026-09-10. Nothing was changed on the host, and no unit was started.

Collected 2026-11-27T20:28:24Z to 20:32:03Z on `w3`, by three short scripts sent over a remote shell.
How the readings were taken: the listing of the env directory, the reads of
`compute.scheduler-dbfree.env` and the user-manager queries need read access to the production user's
files and user manager; the lines below show each command without the prefix that gave that access.
Only file names, two path-valued keys and unit properties were read; no credential-bearing value was
printed. The NFS source is shown with the server's address replaced by `<node-27>`.

## 1. Which env file each unit loads

The table that depends on it: "Unit → EnvironmentFile authority of the compute role (#1694)" in
[`infra/env/README.md`](../../../infra/env/README.md), whose first four rows were verified on
2026-09-02 on the retired node-22 host.

```text
$ systemctl --user show nhms-compute-scheduler.service -p EnvironmentFiles
EnvironmentFiles=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env (ignore_errors=no)
EnvironmentFiles=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env (ignore_errors=no)
$ systemctl --user show nhms-scheduler-evidence-retention.service -p EnvironmentFiles
EnvironmentFiles=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env (ignore_errors=no)
$ systemctl --user show nhms-scheduler-file-provider-refresh.service -p EnvironmentFiles
EnvironmentFiles=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-provider-refresh.env (ignore_errors=no)
$ systemctl --user show nhms-slurm-gateway.service -p EnvironmentFiles
EnvironmentFiles=/scratch/frd_muziyao/NWM/infra/env/compute.host.env (ignore_errors=no)
EnvironmentFiles=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env (ignore_errors=no)
$ systemctl --user show nhms-node22-unit-failure-alert@receipt-read.service -p EnvironmentFiles -p ActiveState
EnvironmentFiles=/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env (ignore_errors=yes)
ActiveState=inactive
```

The five units load the files the table names. The alert unit is a template: its property was read
through an instance name that is not running (`show` loads the unit description; it starts nothing).

## 2. The env directory holds no `compute.env`

The note that depends on it: the `infra/env/compute.env` bullet of the same section.

```text
$ ls -1 /scratch/frd_muziyao/NWM/infra/env | grep -v -E '\.example$|^README\.md$'
compute.host.env
compute.replay.env
compute.replay.env.bak-3191-20261118T0731Z
compute.scheduler-dbfree.env
compute.scheduler-dbfree.env.bak-3191-20261118T0731Z
compute.scheduler-provider-refresh.env
node22-unit-failure-alert.env
$ test -e /scratch/frd_muziyao/NWM/infra/env/compute.env && echo present || echo absent
absent
```

The leftover file of the retired docker/compose lane, and the `DATABASE_URL` it carries on the retired
node-22 host, do not exist in the production checkout on this host. Two backup copies do sit in the
directory; the README's "Backup copies" section says where such copies belong.

## 3. Two keys of the scheduler's live env file

The command that depends on them: the canonical-precip backfill recovery in
`docs/runbooks/current-production-ops.md` §5.3 (read on the retired node-22 host on 2026-09-10).

```text
$ grep -n -E '^(OBJECT_STORE_ROOT|NHMS_OBJECT_STORE_COPYBACK_ROOT)=' /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
13:OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
15:NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
$ stat -c '%A %U:%G' /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
-rw------- frd_muziyao:huser
$ cd /scratch/frd_muziyao/NWM/infra/env && grep -l "^NHMS_OBJECT_STORE_COPYBACK_ROOT=" -- *.env
compute.host.env
compute.replay.env
compute.scheduler-dbfree.env
```

Both keys are present, at the same lines and with the same values as in the 2026-09-10 reading, and
the same three files carry the copyback root.

## 4. The copyback root: mount and permission chain

The rules that depend on it: the copyback lock notes of the same README (a host death holds an NFS
lock until the lease expires; `rm -f` of the lock file needs no ownership while the chain has no
sticky bit).

```text
$ findmnt -no FSTYPE /ghdc/data
nfs4
$ findmnt -no SOURCE /ghdc/data
<node-27>:/home/ghdc
$ findmnt -no OPTIONS /ghdc/data | tr ',' '\n' | grep -E '^vers='
vers=4.2
$ stat -c '%A %a %U(%u):%G(%g) %n' /ghdc /ghdc/data /ghdc/data/nwm /ghdc/data/nwm/object-store
drwxr-xr-x 755 root(0):root(0) /ghdc
drwxrwxrwx 777 root(0):root(0) /ghdc/data
drwxrwxr-x 775 frd_muziyao(1103):huser(1078) /ghdc/data/nwm
drwxrwxr-x 775 frd_muziyao(1103):huser(1078) /ghdc/data/nwm/object-store
$ id -u frd_muziyao; id -gn frd_muziyao; id -g frd_muziyao
1103
huser
1078
```

An NFSv4.2 mount of node-27's export, as on the retired host; the four modes and owners equal the
2026-09-10 reading, with no sticky bit on the chain. `w2` was not read.
