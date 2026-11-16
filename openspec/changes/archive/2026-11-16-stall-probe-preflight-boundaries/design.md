# Design: stall-probe-preflight-boundaries

## Context

`grade()` slot 10 (#2912) fires `scheduler_root_blocked` when the contiguous newest-first run of passes whose
`execution_boundary` is exactly `scheduler_root_preflight_blocked` spans `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`.
The writer has seven `preflight_blocked` dict literals with a literal `execution_boundary`: root (two sites),
`db_free_runtime_preflight_blocked`, `scheduler_rollback_fence_prepared`, `db_free_registry_blocked` and
`scheduler_timing_level_unrecognised` carry no `progress_guard`; `scheduler_progress_guard_blocked` writes its own
guard block. The post-candidate boundaries (`evidence_preflight_blocked`, `slurm_preflight_blocked`, bare
`preflight_blocked`, `db_free_journal_write_blocked`) are assigned to a variable and folded into terminal evidence
that carries the guard, so the blocked streak already counts them.

## Goals / Non-Goals

**Goals:** every guard-less pre-candidate boundary whose artifact lands raises a non-zero verdict once it persists;
the `/ghdc` flip never reads `ok`; a new such boundary in the writer turns a test red.

**Non-Goals:** a new non-neutral pass shape (rejected in #2912 D3: it bends the in-flight gate and frontier); changing
the writer; the post-candidate guarded boundaries; `scheduler_timing_level_unrecognised` (artifact never lands); node-22
deployment.

## Decisions

### D1 One run over the whole boundary set, verdict by the newest boundary

The issue suggests counting the run "per remedy class, from the newest pass". Taken literally that run restarts at the
root -> `db_free_runtime_preflight_blocked` flip, so the probe would read `ok` for up to a threshold after the flip,
which contradicts the issue's own replay acceptance item ("翻到 db_free_runtime_preflight_blocked 后探针不回到 ok").
The run is therefore contiguous over the SET of table boundaries: every member is a pass refused before candidate
selection, so zero submissions hold across the whole span whichever preflight refused each pass. The remedy is what
the operator must fix now, so the newest pass's boundary picks the verdict, and `boundary_passes` shows the history
(e.g. 13 root + 2 db-free). The same rule closes the reverse flip (an hour on `/ghdc`, then the roots go too).

### D2 One verdict per remedy class, one shared slot

Per-boundary verdicts (the issue's recommendation) rather than one generic verdict (its alternative): the OnFailure
mail carries the `runbook=` anchor, so with one verdict per class that anchor is the remedy. The code shape is a
single table lookup in slot 10 with no root special case; the four verdicts cannot collide because they are picked by
the same newest pass. Numbered 10, 10b, 10c, 10d in the runbook so verdicts 11-13 keep their numbers.

### D3 Root-blocked receipt block kept exactly; a sibling block carries the graded run

`root_blocked_run` keeps its four keys and its root-exact semantics (issue acceptance: receipt fields do not regress).
It is a prefix of the graded run, so its old firing rule stays sufficient; the graded rule moves to the new block
`preflight_blocked_run`, which carries `remedy_verdict` so slot 10 re-derives from the receipt alone. The only observable
change to `scheduler_root_blocked` is that it can now fire when a root run follows another table boundary and the
combined span meets the threshold (strictly more alerting, never less).

### D4 Rollback fence alerts

The issue left open whether `scheduler_rollback_fence_prepared` should be exempt as a deliberate pause. The fence is
written by `prepare-file-journal-rollback`, whose runbook requires the scheduler timer and service to stay stopped
until roll-forward consumes the fence; while the timer is stopped the probe already reports `timer_stopped` (slot 3).
So this verdict appears only when the timer runs while a fence is live (timer resumed before roll-forward, or a failed
roll-forward) or the fence is unreadable — a misoperation, not the expected pause. It stays in the graded table.

### D5 Threshold reuse

All four verdicts share `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` and its reachable-bound check; the variable keeps
its name so no deployed drop-in changes meaning. Per-class thresholds are YAGNI until a class needs a different
calibration.

### D6 Writer parity by AST enumeration

The test walks every `ast.Dict` in `scheduler_runtime.py` whose `"status"` is `"preflight_blocked"`, requires a string
constant `"execution_boundary"`, and partitions by the presence of a `"progress_guard"` key. Guard-less == table ∪
`{scheduler_timing_level_unrecognised}`; guarded == `{scheduler_progress_guard_blocked}`. A companion test feeds
mutated source (a new guard-less literal, a new guarded literal, a non-literal boundary) and expects a failure. The
test's node id joins the `scheduler_runtime.py` stop rule in `scripts/select_ci_tests_rules.py`.

## Risks / Trade-offs

- An operator-intended long fence with the timer running now mails every probe tick past the threshold. Accepted per
  D4: the runbook forbids that geometry.
- A pass that is `preflight_blocked` for a reason the table does not name still ends the run; the parity test is what
  keeps the table complete.
