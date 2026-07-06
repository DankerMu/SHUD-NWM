# SUB-11 post-merge live audit receipt (issue #869, Epic #858)

- **Date (UTC)**: 2026-07-06
- **Node-22 HEAD at collection**: `6c739ab4` (post PR #884 merge; equivalent tree to
  pre-squash `23d42e3e` deployed during SUB-10)
- **Resource profile in effect**: post-#882 tune —
  `cpus_per_task=4, memory_gb=8, max_concurrent=32, shud_threads=4`.
- **§10.1 candidate-level pass**: collected as stage-level (see amendment below).
- **§10.2 retention fire receipt**: 2 receipts (1 natural + 1 manual) captured.
- **§10.3 Top-3 python-time ranking**: attached below.

## Amendment to SUB-10 receipt

SUB-10 receipt (`docs/runbooks/receipts/issue-868-node22-sub10-implementation-gate-2026-07-05.md`)
§9.5 stated: *"the pass at 21:47:19 CST (still running at env-revert time) keeps
candidate for its lifetime; the next pass entry re-reads env and runs at stage."*

**Fact correction**: that pass (`scheduler_2026070513_6c4966b16c4a`) never wrote a
`pass:finished` record — it was terminated by `SIGTERM` at 13:53:59Z (~6:41 into a
candidate-level pass) after the operator stopped `nhms-compute-scheduler.timer` to
collect the candidate-level receipt. The timer stop was not reversed inside the
SUB-10 session, so the scheduler business was actually paused between
13:47:11Z (`scheduler.timer` `Stopped`) and 14:14:23Z (`Started` again in
this session at user request). No completed candidate-level evidence JSON
exists for that stopped pass.

Impact: `receipts/instrument-live-<utc>.json` (§10.1 as originally scoped) does
not exist for a candidate-level pass. The invariant check has been performed
against three completed **stage-level** substantive passes captured after the
timer was restarted (see §10.1 below). Candidate-level re-collection is
deferred as a low-priority followup because:

1. The pass-level invariant `python_time_ms + slurm_wait_ms == total_wall_ms`
   holds identically at stage-level (it is the same measured quantity), and
2. Candidate-level adds only inner sub-spans; it does not introduce a new
   pass-level invariant to verify.

## §10.1 pass-level invariant across 3 substantive passes

All three passes ran under post-#882 profile (32×4). Invariant delta = 0 ms in
every case; slurm-wait share is negligible (< 2 %).

| pass_id | level | wall (min) | python_time_ms | slurm_wait_ms | Δ (ms) | slurm share |
|---|---|---|---|---|---|---|
| `scheduler_2026070602_4327d37b0d01` | stage | 51 | 3,071,252 | 21,915 | 0 | 0.7 % |
| `scheduler_2026070603_95f8fbca053b` | stage | 194 | 11,653,576 | 11,541 | 0 | 0.1 % |
| `scheduler_2026070607_e603936c2860` | stage | 136 | 8,061,052 | 134,059 | 0 | 1.6 % |

Verification query (jq run against
`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_<pass_id>.json`):

```jq
.timing.pass | {
  pass_id,
  wall_min: (.total_wall_ms / 60000 | floor),
  invariant_delta_ms: (.python_time_ms + .slurm_wait_ms - .total_wall_ms | floor)
}
```

All three passes yield `invariant_delta_ms: 0`. `schema_version` is
`"nhms.scheduler_pass_timing.v1"` at every level (`timing`, `timing.pass`,
`timing.stages[]`).

## §10.2 retention fire receipts

Two receipts captured on 2026-07-06 in
`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/retention/`:

| filename | trigger | UTC ts | deleted | skipped | total_after (MB) |
|---|---|---|---|---|---|
| `retention-20260706T043435Z.json` | timer natural fire | 04:34:35Z | see file | see file | see file |
| `retention-20260706T111139Z.json` | manual `systemctl --user start …service` | 11:11:39Z | 6 | 3 | 511.3 |

The manual receipt (`retention-20260706T111139Z.json`) summary:

- `deleted_count: 6` — all `pass: "size"` (size-eviction of oldest
  `scheduler_2026070*.json` files ~1.6 MB each dated 2026-07-03).
- `skipped_count: 3` — all `pass: "unrecognised"` (2 `repair_stale_*` diagnostic
  files + 1 `stale-lock-clear-issue882-*.json`); these are correctly left in
  place by the whitelist.
- `total_before_bytes: 546,047,253` → `total_after_bytes: 536,185,467`
  (≈ 511.3 MB, **under** the `NHMS_SCHEDULER_EVIDENCE_MAX_MB=512` cap).
- `partial_failure: false`; `schema_version:
  "nhms.node22_scheduler_evidence_retention.v1"`; policy in effect
  `max_mb=512, retention_days=90, receipt_retention_days=180, whitelist_globs=[]`.
- `receipt_pass: []` — no receipt-retention deletions (all receipts are within
  the 180 d window).

Sub-directory `retention/` was created on first fire per §7.10 design.

## §10.3 Top-3 python-time consumers ranking

Aggregated over the same 3 substantive passes from §10.1 (`timing.stages[]`
records). Ranking is by mean `python_time_ms` per (`source_id`, `stage_name`).

| Rank | source_id | stage_name | count | mean python_ms | mean (min) | total cumulative (min) |
|---:|---|---|---:|---:|---:|---:|
| 1 | gfs | forecast | 26 | 1,499,372 | 25.0 | 649 |
| 2 | gfs | forcing | 26 | 1,396,896 | 23.3 | 605 |
| 3 | IFS | forcing | 26 | 1,256,003 | 20.9 | 544 |
| 4 | IFS | forecast | 36 | 1,158,746 | 19.3 | 695 |
| 5 | IFS | convert | 26 | 1,012,238 | 16.9 | 439 |
| 6 | gfs | state_save_qc | 26 | 974,903 | 16.2 | 422 |
| 7 | gfs | convert | 13 | 928,242 | 15.5 | 201 |
| 8 | IFS | state_save_qc | 36 | 828,710 | 13.8 | 497 |

### Findings that invalidate the original change #2 hypothesis

- **Slurm queue wait is not the bottleneck**. Pass-level `slurm_wait_ms` share is
  0.1 % – 1.6 % of wall-clock across all three passes. The original change #2
  target 1 ("单 pass 无界吞积压" framed as slurm queue backlog) is not supported
  by evidence.
- **`state_save_qc` is not top-3**. It ranks 6/8, not 1/3. The original change
  #2 target 2 ("state_save_qc %1 串行节流") is not the dominant python-time
  consumer.
- **`dispatch_ms` inside forecast/forcing stages is dominant**. In the sample
  stage row we inspected, `python_time_ms == dispatch_ms`, meaning the entire
  python-side stage timing is absorbed into `_submit_and_wait` (submit +
  subprocess-block-wait + poll + result gather). Without finer-grained
  sub-spans inside dispatch, we cannot separate SHUD subprocess block-wait
  (out of mandate per "不含 SHUD 运行时长") from python-side wrapper
  overhead.

### Decision on change #2

Per user directive on 2026-07-06, change #2 (scheduler optimisation) is
**not scoped for now**. Rationale: the instrumentation confirms the addressable
optimisation space (python-side wrapper overhead vs SHUD subprocess wait)
cannot be separated with the current spans; a diagnostic sub-span decomposition
was considered but declined; the mandate excludes SHUD runtime tuning.
The Epic #858 instrumentation resource itself is retained on production and
will continue to accrue `timing:` evidence on every pass, so re-opening this
question later is a data query, not a fresh instrumentation change.

Related: #883 (Slurm `friends/level2` `MaxJobsPA=1` association-limit
concurrency cap) was closed on 2026-07-06 as configuration-side, not scheduler
code work — this cap is one plausible non-scheduler contributor to the
`dispatch_ms` share but confirming it requires an ops coordination step that
is out of scope here.

## Acceptance verification (Epic #858 level)

Referring to Epic #858 body Acceptance section:

| Criterion | Status | Evidence |
|---|---|---|
| All 11 sub-issues closed | 10 closed pre-this-PR (#859–#868); **#869 closes on this PR merge** | See sub-issue links in Epic body |
| `openspec validate` passes | ✓ | `openspec validate instrument-node22-scheduler-pass-timing --strict --no-interactive` valid |
| Live receipt candidate-level invariant | **Substituted with stage-level (see amendment)** | §10.1 above; delta = 0 across 3 passes |
| Retention timer active + 24 h fire receipt | ✓ | §10.2 above; 2 receipts captured |
| Top-3 ranking attached to change #2 | **N/A** — change #2 cancelled | §10.3 above; ranking preserved for future reference |
