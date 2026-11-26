# Retry path divergences are intended (#3319)

## Why

The retry accounting table (`tests/test_retry_accounting_table.py`, #3264) pins two differences between
retry paths "as they are", and the owner module's docstrings defer them to #3319. Nobody had decided
whether they were deliberate. The owner decided on 2026-11-26: both stay.

## What changes

No code path changes. The decision is written down where the next reader looks for it.

- `job-retry-mechanism` gains two requirements that state the behaviour as intended, with the values
  the table already asserts:
  - the non-master automatic retry appends to the unstripped job id and counts from the caller's
    snapshot; the journal's derivation for a current-contract master strips the last suffix and counts
    from the effective attempt (the cycle's inline retry counts with the inline formula, not decided here);
  - after an upstream refresh a terminal stage is run again only from `failed`, `submission_failed`
    or `partially_failed`.
- The docstrings and comments that say "#3319 owns any behaviour change" say what was decided, or, for
  the differences #3319 did not decide, that a change needs its own behaviour-change issue.

## Decisions

- **D1. Keep stacking on the non-master path.** Stacked ids (`..._retry_1_retry_2`) are the documented
  production shape (#2254), every parser takes the last suffix, live journals hold stacked rows, and
  no incident asks for a change. Stripping would have to prove, for every consumer, that no occupied
  id is minted again and no stage's attempt count restarts. The count source (the caller snapshot's
  `retry_count`) and the missing lower bound stay with it.
- **D2. Keep the upstream-refresh set.** `cancelled` is an operator's intent and `permanently_failed`
  a spent budget or a non-transient error; a refreshed upstream does not override either. The spec
  already says so for marked master rows ("Marked master rows do not resurrect via upstream
  refresh"); the new requirement names the whole set.
- **D3. Two existing requirements are modified so that nothing contradicts the new ones.** "Retry
  Identity — Same run_id, New slurm_job_id" says `n` "is at least 1"; the non-master path can mint
  `_retry_0`, so the scenario names that exception. The scenario "Divergent paths are pinned as they
  are" of "Retry and attempt accounting SHALL have one owner" says which two of its four paths are now
  kept on purpose and that a change edits the matching new requirement too.
- **D4. No test is added for its own sake.** Each scenario below names values the table already
  asserts; the implementer maps scenario to row and adds a row only where none exists.

## Not in this change

- The inline-retry formula's `failure_number - 1` term, the two `rsplit` increments of the forecast
  cycle, `stage_job_sort_attempt` against `effective_retry_attempt`, and the unclamped
  `next_manual_attempt`: #3319 did not decide them; they stay pinned as they are.
- Any change of a computed id, count or verdict.

## Impact

- Specs: `job-retry-mechanism` (two ADDED and two MODIFIED requirements). `slurm-job-chain` is not
  touched: its requirement "Cycle-stage retry attempt numbering SHALL use the last retry suffix" already
  reads stacked ids by their last suffix and says nothing about which path produces them.
- Code: comments and docstrings in `services/orchestrator/retry_identity.py`; a comment in
  `tests/test_retry_accounting_table.py`.
