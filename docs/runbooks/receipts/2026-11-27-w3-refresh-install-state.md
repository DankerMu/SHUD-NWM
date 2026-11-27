# Receipt: refresh lane and refresh-timer-health install state on w3, 2026-11-27

> Provenance: read-only commands run in an interactive shell on `w3` (the compute control host) for #3413,
> which rewrote `docs/runbooks/production-ops/file-provider-refresh.md`. That page describes the installer
> baseline of the retired node-22 host as recorded for #2294 on 2026-09-25. Nothing was changed on the host.

Collected 2026-11-27T17:34:24Z to 17:34:41Z on `w3`. The unit states were read from the production user's user manager.

## 1. No installer baseline is recorded on w3

`scripts/install_node22_scheduler_file_provider_refresh.sh` keeps its restore baseline under
`/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/install-state/` (`refresh.before` and the two unit
`.before` files); `scripts/install_node22_refresh_timer_health.sh` keeps its own under
`/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/install-state/`.

```text
$ ls -la --time-style=long-iso /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/install-state
ls: cannot access '/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/install-state': No such file or directory
$ ls -la --time-style=long-iso /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/
drwx------   2 frd_muziyao huser  4096 2026-11-27 02:52 emergency
drwx------   3 frd_muziyao huser  4096 2026-11-27 02:52 receipts
-rw-------   1 frd_muziyao huser     0 2026-11-06 02:41 .refresh.lock
drwx------   2 frd_muziyao huser  4096 2026-11-27 02:52 runs
$ ls -la --time-style=long-iso /scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/
drwx------   2 frd_muziyao huser  4096 2026-11-27 17:01 receipts
```

(The `.` and `..` entries and the `total` line are left out.) Neither `install-state` directory exists: on this host
the units were not put in place by a recorded `--install`, and there is no `refresh.before`, no `scheduler.before`
and no health-probe baseline.

## 2. Both lanes are armed

```text
$ systemctl --user show <unit> -p UnitFileState -p ActiveState
nhms-scheduler-file-provider-refresh.timer    ActiveState=active    UnitFileState=enabled
nhms-scheduler-file-provider-refresh.service  ActiveState=inactive  UnitFileState=static
nhms-node22-refresh-timer-health.timer        ActiveState=active    UnitFileState=enabled
nhms-node22-refresh-timer-health.service      ActiveState=inactive  UnitFileState=static
```

The deployed unit files are the repository's: the drift probe of 2026-11-26 reported `same` for all four unit files
([receipt](2026-11-26-issue-3394-compute-api-unit-retired.md)).
