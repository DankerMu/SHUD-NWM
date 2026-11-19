# Cut the idle scheduler pass cost: no per-event evidence in completion scoring, bounded retention skips (#3223)

## Why

An idle node-22 scheduler pass takes about 400 s of one core, every five minutes, to conclude "nothing to do"
(281 of 284 retained passes were idle on 2026-11-19). A py-spy profile of one idle production pass (9,440 samples,
master c18f131fc, recorded on #3223) puts 83.9% of it in `scheduler_discovery._cycle_completion_verdict`, the
per-model loop that re-scores cycles that are already complete (8 cycles x 66 models = 528 candidates per pass).
Broken down by calling line:

- **41.1% of the whole pass is one line**: `scheduler_state_evidence_owner.py:61`,
  `events = [_evidence_safe(event) for event in _state_events(state)]`. It builds a redacted copy of every pipeline
  event for `decision.evidence["pipeline_events"]`. Completion scoring reads `decision.reason` and three evidence fields
  (`terminal_source`, `hydro_run`, `run_manifest_initial_state`); it never reads that copy and never publishes it.
- 37.5% is the journal's `candidate_state` (containment-checked fingerprint, public sanitising, row reduction).
  Not touched here: every part of it is bound by the #1567 containment and #1661 cache rules.
- Retention is 0.3% of the time but 1.77 MB of each 2.4 MB pass evidence file: one `skipped` entry per retained key
  (11,414 of them, almost all `within_retention_window`).

## What changes

1. Completion scoring asks the same decision ladder for a decision **without the per-event evidence copy**. The ladder
   itself, the state it reads and every other evidence field are unchanged, so the verdict is unchanged.
2. The submitting path (`scheduler_candidates.py`) is not touched: its decisions and its published evidence stay
   byte-identical, `pipeline_events` included.
3. Scheduler pass evidence records retention skips as per-reason counts plus a bounded sample of the skips that are
   not the normal `within_retention_window` case, instead of one entry per key.

## Fixture

```text
Issue type: refactor (performance, no verdict change) + evidence format change
Fixture level: expanded
Upstream suggested level: absent
Blast radius: a wrong completion verdict makes the backfill window stall on a finished cycle or skip an unfinished one;
              a wrong retention block hides an abnormal skip from the operator
Selected risk packs: Schema / field names; Resource limits / large input; Legacy compatibility; Concurrency / shared state (not selected, see tasks)
Evidence floor: verdict/decision parity test incl. a recalibrated-model cycle; submit-path evidence unchanged; retention evidence tests;
                node-22 idle-pass timing and evidence size before/after (post-merge)
```

## Decisions

- D1. One ladder, one switch. `_candidate_state_decision` gains an opt-out for the event evidence copy (keyword, default
  keeps today's behaviour). With the opt-out the key `pipeline_events` is **absent** from `decision.evidence`, not an
  empty list, so a reader cannot mistake "not built" for "no events". Nothing else in the evidence changes.
- D2. Only `_cycle_completion_verdict` uses the opt-out. Its `context.candidate_state_decider` call passes it; test
  doubles that take two positional arguments keep working (the implementer chooses how: a second context slot, or a
  call that tolerates deciders without the keyword; no production decider may silently ignore it).
- D3. No cache, no new read path, no change to `candidate_state`, `_cycle_rows`, `_completion_scope`, the strict /
  successor providers or `_terminal_init_state_verdict`. #1567, #1661 and #1735 are preserved by not touching them.
- D4. Retention in pass evidence: `counts` stays; `skipped` (the per-key list) is replaced by `skipped_by_reason`
  (reason -> count, every reason present in the run) and `skipped_sample` (the first 50 skipped entries whose reason is
  not `within_retention_window`, in the order retention produced them) plus `skipped_sample_omitted` (how many such
  entries did not fit). `planned`, `deleted`, `failed`, `frontier`, `extra_roots`, `copyback_lock_failures` are unchanged.
  The existing over-limit compaction keeps `skipped_by_reason` (a small scalar map) and may drop the sample.
- D5. `RetentionResult` keeps the full list in memory (`frontier()` derives `protected_count` from it); only what the
  scheduler writes into pass evidence is projected. A standalone retention receipt, if one exists, is out of scope and
  must stay as it is: the implementer reports which callers of `to_dict()` exist.
- D6. Out of scope, recorded on #3223 for #3227: the journal-side 37.5% (one read per cycle, raw state for verdicts),
  and not re-scoring settled cycles at all.

design.md follows.
