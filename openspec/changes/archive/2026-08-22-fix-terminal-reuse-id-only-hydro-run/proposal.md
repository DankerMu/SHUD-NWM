# Reuse terminal success for id-only hydro_run rows when the run manifest proves the init state

## Why

In the db-free (file-journal) production deployment every already-`succeeded`
candidate of a revisited cycle is re-run from scratch, forever. node-22 pass
`scheduler_2026082207_dc7374b257be` recorded 30/30 candidates decided
`strict_warm_start_terminal_init_state_mismatch`, `skipped_candidate_count: 0`,
60 job submissions and 1.82 h of wall clock — recomputing models that had been
`succeeded` since 2026-08-13, and repeating it on the very next pass.

The chain is a single predicate. The candidate admission ladder's only reuse
exit (`services/orchestrator/scheduler_candidates.py:470-515`) is gated on
`_terminal_decision_matches_strict_warm_start`, whose `hydro_run` leg is
`_warm_state_record_matches` (`:2018`) — a **selected-driven** four-field
equality over `state_id`/`checksum`/`uri`/`valid_time`. The file journal's two
`hydro_run` writers — `create_hydro_run` and `create_hydro_run_from_basin` in
`services/orchestrator/file_orchestration_journal.py`, the only two callers of
`_write_hydro_run` — persist only `init_state_id` + `quality`. Three of the four fields are
therefore structurally absent on every row this deployment has ever written, so
the equality fails on `checksum` even when the recorded `state_id` is
character-for-character the state strict warm start just selected. Mismatch
routes to `_strict_warm_start_terminal_mismatch_decision` (`:2268`), which
resubmits native SHUD with `durable_output_reused: False`.

The #1173 retry budget does not converge this: each resubmission *succeeds* and
writes back another id-only row, so `attempt` stays `0` (30/30 measured) and the
loop is unbounded; were it ever to bind, the terminal state is 30 candidates
`blocked_strict_warm_start_init_state_mismatch` + `manual_retry_required`, which
in db-free is an empty promise (#1186 / #1555).

The same pass already treats the same row as current on the verdict side:
`terminal_init_state_match` (`services/orchestrator/scheduler_init_state_match.py:81-92`)
is observed-driven and classifies an id-only row `match`. Two truths for one row
in one pass.

`3b587c55` introduced the strict comparison for a real reason — a repaired
checkpoint keeps its deterministic `state_id` while its checksum changes — and
that protection must survive. It survives because the run manifest carries the
full four-field init state (`run_manifest_initial_state`, measured on node-22 to
include `checksum` / `ic_file_uri` / `valid_time`): an id-only terminal row is
admitted for reuse only when the run manifest affirmatively proves the four
fields, never on the id alone.

## What changes

- The candidate ladder's `hydro_run` leg gains one narrow upgrade: when the
  recorded `init_state_id` equals the selected state's and the remaining
  identity fields are **absent** (not disagreeing), the decision consults
  `run_manifest_initial_state`; a full four-field match admits the existing
  reuse exit, and anything else (manifest absent, or any field disagreeing)
  keeps today's **budgeted** `strict_warm_start_terminal_init_state_mismatch`
  decision byte-identical.
- The `strict-warm-start` spec requirement that pins the candidate leg to
  selected-driven semantics is modified to carry this upgrade, including the
  explicit prohibition it exists to enforce: the upgrade never reroutes onto the
  unbudgeted `strict_warm_start_terminal_run_manifest_missing` path.
- A regression guard pins candidate-side and verdict-side conclusions together
  for the shapes where they must agree, so the two rules cannot drift apart again.
- The three db-free-dead dedup gates named in the issue receive a recorded
  verdict backed by an audit of every `candidate_state` implementer.

## Non-goals

- Widening the file journal's `hydro_run` rows (the issue's alternative): the
  backlog rows stay id-only, so the transition logic would still be required.
- #1735 lineage/completion attribution, #1734 journal read amplification, the
  sbatch StdOut attribution defect, cold-start policy, `state_id` derivation.
