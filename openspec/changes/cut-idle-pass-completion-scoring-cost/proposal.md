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
3. Scheduler pass evidence stops listing the skips whose reason is `within_retention_window` one by one (11,124 of
   11,420 entries and 1.76 of 1.84 MB in a production pass measured 2026-11-19); it records them as counts per root.
   Every other skip stays listed as today.

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
- D2. Only `_cycle_completion_verdict` uses the opt-out, at both of its `context.candidate_state_decider` calls
  (`scheduler_discovery.py:362` and `:444`). The keyword is passed unconditionally: no `TypeError` fallback, no
  signature sniffing, so a decider cannot silently ignore it. The discovery and submit contexts are separate objects
  bound to the same function (`scheduler_core.py:562`, `:942`), so the submit path keeps the default. The compat wrapper
  already forwards `**kwargs` (`scheduler_state_compat.py:226-229`). The one two-argument decider double on the discovery
  slot (`tests/test_file_orchestration_journal.py:7552`) is updated to accept the keyword.
- D3. No cache, no new read path, no change to `candidate_state`, `_cycle_rows`, `_completion_scope`, the strict /
  successor providers or `_terminal_init_state_verdict`. #1567, #1661 and #1735 are preserved by not touching them.
- D4. Retention in pass evidence (revised after fixture review: the first draft's 50-entry sample contradicted
  "each exempted entry is recorded in the retention receipt's skipped list" and could crowd out abnormal skips).
  `retention.skipped` keeps every skipped entry **except** those whose reason is `within_retention_window`; frontier
  exemptions, protected static assets, symlinked roots, unparseable names, recently modified Slurm logs and
  root-admission failures stay listed exactly as today. Two fields are added: `skipped_summary`, one row
  `{root, reason, count}` per (root, reason) pair over **all** skips, whose counts sum to `counts.skipped`; and
  `skipped_omits: ["within_retention_window"]`, so a reader can tell the list is partial. `schema_version` stays
  `nhms.production_scheduler.retention.v2` (additive fields); `counts`, `planned`, `deleted`, `failed`, `frontier`,
  `extra_roots`, `copyback_lock_failures` are unchanged. The existing over-limit compaction keeps `skipped_summary`
  and `skipped_omits`.
- D5. `RetentionResult` keeps the full list in memory (`frontier()` derives `protected_count` from it) and
  `to_dict()` is unchanged: the standalone retention receipt (`services/orchestrator/cli.py:223`) keeps listing every
  entry. Only what the scheduler writes into pass evidence is projected.
- D6. Out of scope: the journal-side 37.5% (one read per cycle, no public sanitising of a state that is only
  scored) and not re-scoring settled cycles at all. A follow-up performance issue is filed with the E6 numbers.

design.md follows.
