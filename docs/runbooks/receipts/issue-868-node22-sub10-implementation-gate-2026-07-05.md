# SUB-10 Implementation-merge gate receipt (issue #868, Epic #858)

- **Date (UTC)**: 2026-07-05
- **Master HEAD deployed to node-22**: `f7137eb` (after SUB-9 merge log commit)
- **Env deploy timestamp (UTC)**: 2026-07-05T11:54:02Z
- **Env revert timestamp (UTC)**: ~2026-07-05T13:52:00Z (2h after deploy)

## §8 Local verification gate (on local Mac at HEAD `f7137eb`)

| Task | Command | Outcome |
|---|---|---|
| 8.1 | `uv run ruff check .` | `All checks passed!` |
| 8.2 | `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py tests/test_scheduler_timing.py tests/test_scheduler_evidence_retention.py` | `1046 passed in 66.42s (0:01:06)` |
| 8.3 | `openspec validate instrument-node22-scheduler-pass-timing --strict --no-interactive` | `Change 'instrument-node22-scheduler-pass-timing' is valid` |

## §9 Implementation-merge gate (node-22 `frd_muziyao@210.77.77.22`)

### §9.1 Pull master + deploy retention timer

```
cd /scratch/frd_muziyao/NWM
git pull --ff-only
# → ff clean, no untracked blockers, no stash pop needed
mkdir -p ~/.config/systemd/user
cp infra/systemd/nhms-scheduler-evidence-retention.service ~/.config/systemd/user/
cp infra/systemd/nhms-scheduler-evidence-retention.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nhms-scheduler-evidence-retention.timer
# → Created symlink /users/frd_muziyao/.config/systemd/user/default.target.wants/
#     nhms-scheduler-evidence-retention.timer → …/nhms-scheduler-evidence-retention.timer
echo 'NHMS_SCHEDULER_TIMING_LEVEL=candidate' >> infra/env/compute.scheduler-dbfree.env
# (pre-backup: infra/env/compute.scheduler-dbfree.env.pre-sub10)
```

Node-22 HEAD after pull:

```
f7137eb Log #867 SUB-9 retention unit tests merge
b202f5b Add node-22 scheduler evidence retention unit tests (#867) (#879)
10c9283 Log #866 SUB-8 systemd retention units merge
```

### §9.2 & §9.3 Running-state proof — timing collector single-line JSON pass:started

**Note on receipt path**:
The task text mentions `journalctl --user -u nhms-compute-scheduler.service`,
but the currently-deployed `.service` unit routes stdout to
`StandardOutput=append:/scratch/frd_muziyao/nhms-prod/workspace/scheduler/logs/nhms-compute-scheduler.log`
rather than to journal. The receipt therefore quotes the log file
(same content, same schema, same `jq` parseability).
Redirecting `.service` StandardOutput to journal is a follow-up
hardening independent of SUB-10's merge gate.

Log file: `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/logs/nhms-compute-scheduler.log`
Total `phase` matches since 21:38: 53 (multiple pass-started/finished + one stage:started span).

Raw excerpts (last 8 phase records at capture time, all single-line JSON parsing cleanly under `jq`):

```json
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:38:38.669382Z", "pass_id": "scheduler_2026070513_4256c98082c7", "level": "candidate", "phase": "stage:started", "stage_name": "convert", "source_id": "gfs", "cycle_id": "gfs_2026063000"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:38:43.896009Z", "pass_id": "scheduler_2026070513_500b1094738a", "level": "candidate", "phase": "pass:started"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:38:43.926859Z", "pass_id": "scheduler_2026070513_500b1094738a", "level": "candidate", "phase": "pass:finished"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:45:04.590740Z", "pass_id": "scheduler_2026070513_0d1e6d2d8b5f", "level": "candidate", "phase": "pass:started"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:45:04.621341Z", "pass_id": "scheduler_2026070513_0d1e6d2d8b5f", "level": "candidate", "phase": "pass:finished"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:45:40.937480Z", "pass_id": "scheduler_2026070513_b8a3573ac2f8", "level": "candidate", "phase": "pass:started"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:45:40.975070Z", "pass_id": "scheduler_2026070513_b8a3573ac2f8", "level": "candidate", "phase": "pass:finished"}
{"schema_version": "nhms.scheduler_pass_timing.v1", "ts": "2026-07-05T13:47:19.683348Z", "pass_id": "scheduler_2026070513_6c4966b16c4a", "level": "candidate", "phase": "pass:started"}
```

`jq` projection (verifies parse):

```
$ grep 'pass:started' <log> | tail -3 | jq -c '{schema_version, pass_id, level, phase}'
{"schema_version":"nhms.scheduler_pass_timing.v1","pass_id":"scheduler_2026070513_0d1e6d2d8b5f","level":"candidate","phase":"pass:started"}
{"schema_version":"nhms.scheduler_pass_timing.v1","pass_id":"scheduler_2026070513_b8a3573ac2f8","level":"candidate","phase":"pass:started"}
{"schema_version":"nhms.scheduler_pass_timing.v1","pass_id":"scheduler_2026070513_6c4966b16c4a","level":"candidate","phase":"pass:started"}
```

Field shape verified:

- `schema_version == "nhms.scheduler_pass_timing.v1"` (matches SUB-1 §1.1)
- `pass_id` matches `scheduler_<cycle>_<hex12>` (SUB-1 §1.4 + `scheduler_runtime.py:469`)
- `level == "candidate"` (env override picked up by pass entry per D4)
- `phase ∈ {pass:started, pass:finished, stage:started, stage:finished}` (SUB-1 §1.4)
- Stage record additionally carries `stage_name`, `source_id`, `cycle_id` (SUB-2 §2.2 stage span)

All records are UTC-suffixed `Z`, ISO 8601, single-line JSON, terminated `\n`. `jq -c` round-trip preserves shape.

### §9.4 Retention timer active + list-timers next-fire

```
$ systemctl --user is-active nhms-scheduler-evidence-retention.timer
active

$ systemctl --user list-timers | grep nhms-scheduler-evidence-retention
Mon 2026-07-06 12:23:14 CST  14h -                                 - nhms-scheduler-evidence-retention.timer nhms-scheduler-evidence-retention.service
```

Next-fire timestamp `2026-07-06 12:23:14 CST` = `2026-07-06 04:23:14 UTC`, i.e. ~14 h from deploy — inside the required ≤24 h window. Delta from ideal `04:15:00 UTC` = ~8 min, within `RandomizedDelaySec=15m` jitter.

### §9.5 Env reverted to default (stage)

`sed -i.bak-sub10` replaced the appended line with a commented-out marker so subsequent passes see NO `NHMS_SCHEDULER_TIMING_LEVEL` env var → SUB-1 §1.2 default `stage`. Backup preserved as `infra/env/compute.scheduler-dbfree.env.bak-sub10`.

Final env-file line:

```
# NHMS_SCHEDULER_TIMING_LEVEL=stage # reverted from candidate after SUB-10 collection pass at 2026-07-05
```

Per D4 the pass at `21:47:19 CST` (still running at env-revert time) keeps `candidate` for its lifetime; the next pass entry re-reads env and runs at `stage` (default), matching the design-contract steady-state stdout volume.

## Deferred (SUB-11 §10 post-merge audit)

- 10.1 Wait for one full production pass at `candidate` to complete (1h04–2h43 wall-clock) → attach evidence JSON `timing:` block; invariants `python_time_ms + slurm_wait_ms == total_wall_ms ± 5 ms`; per-stage invariant; `pass.slurm_wait_ms == union_ms(stage direct-measured + restart_reconcile)`.
- 10.2 Wait 24 h retention fire → attach `retention/retention-<utc>.json` receipt confirming `retention/` subdir created (first fire) and size within cap.
- 10.3 Top-3 python-time consumer ranking across 3–5 consecutive production passes → input for change #2.
