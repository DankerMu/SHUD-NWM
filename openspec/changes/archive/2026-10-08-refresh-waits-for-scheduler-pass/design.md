# Design

## Why wait, and why it cannot starve

systemd ordering is between start jobs. The refresh unit declares `Before=nhms-compute-scheduler.service`. A
oneshot's start job lasts from `ExecCondition`/`ExecStartPre` to the end of `ExecStart`. So while the refresh
is starting — including the time its wrapper spends waiting — a start of the scheduler service requested by
its timer is queued until the refresh job ends. Observed on node-22 on 2026-10-07 and 2026-10-08 for the
`ExecStart` phase (the next pass started at the second the refresh finished).

A pass that is already running is not affected by the ordering (its start job was dispatched before the
refresh job existed), which is the gap of #2749. With the wait in the wrapper the sequence is:

    pass N running -> refresh job starts, wrapper waits -> pass N ends -> timer requests pass N+1, queued
    behind the refresh job -> refresh runs -> refresh job ends -> pass N+1 starts

The refresh waits for at most one pass. It cannot be overtaken by pass N+1.

It cannot wait on pass N+1 either: a unit whose start job is queued keeps its `ActiveState`, so `is-active`
prints `inactive` (or `failed`) for the queued pass, not `activating`. The scheduler unit on node-22 has
`Restart=no`, `RemainAfterExit=no` and `After=... nhms-scheduler-file-provider-refresh.service` (read with
`systemctl --user show` on 2026-10-08; the unit is not in the repository), so a finished pass is `inactive`
or `failed` and never lingers in `active` or `activating (auto-restart)`. The wrapper's `is-active` call has
no timeout of its own; a hung `systemctl` is ended by `TimeoutStartSec`.

A direct manual run of the wrapper outside systemd has no ordering protection after its wait; that path stays
under the existing operator rule (scheduler timer stopped), as before.

## Where the wait lives

In the wrapper, not in the unit: the unit runs the wrapper from the checkout, so `git pull --ff-only` deploys
the behaviour without the installer cycle (which disarms the lane and is refused while it is armed); and one
implementation serves the unit and a manual wrapper run. `ExecCondition` is removed from the repository's unit
file because a condition that can never fail documents a guard that does not exist.

## Bounds

`TimeoutStartSec=7200` covers the wait and the refresh. Refresh runs observed: about 10 minutes. Wait bound
5400 s leaves 30 minutes. Scheduler passes are usually about 8 minutes; the stall probe's notes record
intervals up to 193.9 minutes, so a pass can outlast the bound. Then the refresh of that day fails with exit 3
(unit `failed`, visible in `systemctl --user --failed` and to the installer's entry gate, which requires
`reset-failed`), and the next day's timer tries again; the timer-health probe alerts on manifest age at 120 h.
Raising `TimeoutStartSec` to cover the longest pass would also need the succession tool's 7500 s start timeout
and the health probes revisited; not done here.

## Unknown state

Fail closed: the old code proceeded when `systemctl` was missing or printed nothing (`2>/dev/null` and a
non-zero exit both meant "not active"). The new code refuses, because proceeding is the unsafe direction.

## Effect on the monitors

- Scheduler stall probe: thresholds are in hours (360 min defaults); the daily gap between passes grows by at
  most one pass duration. No change.
- Refresh timer-health probe: judges the timer's states, next elapse and manifest age (120 h); a refresh that
  starts up to 90 minutes later does not approach any of them. No change.
- Installer entry gate: accepts only `inactive` for the refresh service; a refresh failed by the bound needs
  `reset-failed` before an installer action, as any failed refresh did before.
