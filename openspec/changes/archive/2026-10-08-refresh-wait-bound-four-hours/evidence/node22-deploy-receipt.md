# node-22 deployment receipt (PR #2781, follow-up of #2749)

Host node-22, `/scratch/frd_muziyao/NWM`, 2026-10-08, one session. Times UTC unless marked. Raw files on node-22:
`/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/issue-2749-bound-20261008T074902Z/` (`observe.log` sha256
`faab0caa79c21d093d37f9fde6836676e57c9b2d4604339cf20186f48494f18e`, installer stdout/stderr per action, `start.out`).

| time | step | result |
|---|---|---|
| 07:49:02 | pre-check | refresh service `inactive`; timer next elapse 2026-10-09 10:35:19 CST; worktree clean (untracked `.nhms-work/` only) |
| 07:49 | `git pull --ff-only` | `0ae95094` -> `7ac79243`; wrapper `scheduler_wait_bound_seconds=14400` |
| 07:49 | installer `--rollback`, `--install` | `rolled_back`, `installed_stopped`, both `scheduler_unchanged: true`; installed unit identical to the repository's; `TimeoutStartUSec=6h` |
| 07:49:09 | `systemctl --user start nhms-scheduler-file-provider-refresh.service` while the pass started 07:44:55 was running | wrapper waited (last wait line: 180 s) |
| 07:52:40 | scheduler pass ended | |
| 07:52:44 - 08:02:2x | observer, every 5 s, 116 samples | scheduler service `inactive`; `list-jobs`: refresh `start running`, scheduler `start waiting` |
| 07:52:55 - 08:02:23 | refresh `refresh_20261008T075255Z_393092653a22` | `published`; the next scheduler pass started at 08:02:23 |
| 08:02 | installer `--enable` | `enabled_active`, `scheduler_unchanged: true`; timer enabled/active, next elapse 2026-10-09 10:33:45 CST |
| 08:02:47 | health probe started by hand | verdict `ok`, `stopped_dwell_hours` 8; no failed user unit |

- The timer was disarmed from 07:49 to 08:02; no hourly probe run fell into that span.
- Deviation: the session ran inside the 02:15Z - 08:45Z span named by the change. The pre-check above is why that
  was safe on this day.
- Not shown: a wait longer than 5400 s (covered by the local test on the real bound), and a timer-fired refresh.
