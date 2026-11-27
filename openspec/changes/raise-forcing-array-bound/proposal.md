# Raise the Slurm array concurrency bound from 32 to 64 (#3344)

## Why

Since #3344 a forcing task asks for one CPU. Measured on the pass of 2026-11-26 22:00Z (132 forcing
tasks, receipts on #3344 and #3384): the aggregate bound of 32 lets the whole forcing array fit on w2
(40 cores), w3 takes no forcing task, and a task on the full host takes about 30% more CPU time than on
a half-empty one. The forcing stage takes 44 minutes and the pass ends with the forecast of the model
whose forcing finished last.

Owner decision of 2026-11-27: raise the bound to 64 and measure one pass; go back to 32 if the pass is
not shorter or forecast queueing gets worse.

## What changes

The bound has two sources, and the smaller one wins per array (`services/slurm_gateway/real_backend.py`):

| Source | Now | After |
|---|---|---|
| `NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND` (scheduler env; budgeted over the arrays of one pass) | 32 | 64 |
| `max_concurrent` of the default resource profile (`config/resource_profiles.yaml`; per array) | 32 | 64 |

What 64 means per array (computed with `_array_concurrency_budgets`, worker bound 4):

| Pass shape (tasks per array) | Budgets at 32 | Budgets at 64 |
|---|---|---|
| two sources, 65 / 1 / 65 / 1 | 15 / 1 / 15 / 1 | 31 / 1 / 31 / 1 |
| one source, 65 / 1 | 31 / 1 | 63 / 1 |

With two sources the profile value does not bind (31 is below 32); it binds on a one-source pass, which
is why both values move.

- **Tracked**: the profile value, the env template (`infra/env/compute.scheduler-dbfree.env.example`),
  the runbook lines that state 32.
- **The repair cap follows.** `--repair-missing-forcing` refuses an aggregate bound above 32
  (`services/orchestrator/scheduler_config/config.py`, spec `job-retry-mechanism`). The exact-cycle
  wrapper loads the scheduler's env file, so with 64 in that file every repair would be refused. The cap
  becomes 64: it still refuses a configuration above what production is allowed to run. This is the one
  code behaviour in this change.
- **Host** (after the merge, one pass boundary): fast-forward of the production checkout and the one
  line of the live env file (backup first). No restart: the gateway reads the profile file of the
  checkout on every submission (`services/slurm_gateway/real_backend.py`, no cache), so the new profile
  value is live as soon as the fast-forward lands, and the scheduler reads its env at the start of each
  pass. Going back is the same two steps in reverse.

## Decisions

- **D1. `max_concurrent` cannot be set per job type** (the loader refuses it), so 64 also applies to the
  forecast and `state_save_qc` arrays. Forecast concurrency rises too: at 32 one source's forecast
  array is held by its throttle (15 tasks of 4 cores and one of 12 are 72 cores, below the 80 there
  are), and only when the two sources' forecasts overlap are the cores the limit; at 64 the cores are
  the limit throughout. So the measurement moves more than the forcing stage. `state_save_qc` goes
  from 15 to 31 per array; some of its tasks already wait hundreds of
  seconds with about 10 s of CPU (reported on #3384, cause unknown), which is why its elapsed
  distribution is among the rollback criteria.
- **D2. The code default stays 32** (`scheduler_config/config.py`, used when the env key is unset).
  Production sets the key; the default is what a development run gets.
- **D3. Tests that set the bound to 32 as their own input keep it.** They test the budgeting, not the
  production value.
- **D4. The baseline is the 22:00Z pass**, not the 10:00Z one: the 10:00Z pass of 2026-11-26 was
  GFS-only and ran with SMT still on, so there is no like-for-like 10:00Z number. The measured pass
  has to be a two-source pass of 132 forcing tasks as well; a one-source 22:00Z pass does not count
  and the next two-source one is taken.

## Rollback criteria (decided before the measurement)

Baseline: the pass of 2026-11-26 22:00Z (pass 97 min 46 s, forcing stage 43 min 39 s, forecast stage
70 min 15 s, `state_save_qc` 40 tasks over 60 s, no non-`COMPLETED` job).

**Back to 32 at once**, if on the measured pass any of these holds:

- any `nhms_*` job ends other than `COMPLETED`;
- the pass is 5 minutes or more longer than the baseline;
- the forecast stage is more than 5 minutes longer;
- more than 60 `state_save_qc` tasks take over 60 s;
- a job of another user submitted between the first start and the last end of our jobs starts more
  than 10 minutes after its submission (`sacct -a -X -o user,jobname,submit,start`; the production
  user's `sacct -a` shows the other tenant's jobs on this cluster).

**Keep 64**, if none of the above holds and the pass is at least 5 minutes shorter.

**Read a second pass**, if none of the above holds and the pass is within 5 minutes of the baseline
either way: one pass is one sample and SHUD wall-clock varies with the day's forcing. If the second
pass is not at least 5 minutes shorter than the baseline either, back to 32.

"Back to 32" means the two values; the repair cap may stay at 64.

## Impact

- Spec: `job-retry-mechanism` (the repair cap and the wording of the bound in two scenarios).
- Code: one constant. Config: one value. Template: one value. Docs: three passages.
- Host: a fast-forward and one env line; no restart.

## Fixture

- Fixture level: `standard`.
- Risk packs: deployment/ops, scheduler concurrency.
