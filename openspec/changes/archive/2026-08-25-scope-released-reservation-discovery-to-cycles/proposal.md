# Scope the released-reservation discovery query (issue #1810)

## Why

PR #1802 shipped `query_released_identity_blocked_jobs`
(`services/orchestrator/file_orchestration_journal.py:1421`) as the FIND half of
the operator recovery command for #1748. On node-22 production it does not run:

```
$ nhms-pipeline recover-released-identity-blocked-reservation --journal-root <prod>
exit=2
file_journal_record_limit_exceeded
```

The query does a whole-tree replay, and that replay's aggregate record budget is
`_RecordBudget(max(self.max_records, 1), "pipeline_job_records")`
(`file_orchestration_journal.py:5526`) with `max_records` defaulting to
`MAX_FILE_JOURNAL_RECORDS = 100_000` (`:144`). Production has already crossed it:

| subtree | files | jsonl records |
|---|---|---|
| `journal/` | 232 | **71 213** |
| `latest/` | 4 329 | – |
| `pipeline-jobs/` (flat) | ~4 555 | – |

`_replay_all_pipeline_job_records` consumes one budget unit per `journal/` record
plus one per pipeline job expanded from each `latest/` file. `journal/` is
append-only history, so this is not a transient overshoot — it is already over
and only deepens.

Two things this is NOT:

- **Not a performance problem.** The docstring I wrote for that query defends the
  whole-tree replay on cost grounds ("acceptable because this is an operator
  command run by hand on a wedge, never a scheduler pass"). That answered the
  wrong objection. What fires is a hard fail-closed budget, not wall time.
- **Not a scheduler outage.** Scheduler passes use
  `_iter_reconcile_pipeline_job_records` (inventory-scoped) and are unaffected;
  the post-deploy receipt on node-22 shows 14 submitted / 34 terminal-skipped /
  0 blocked, identical to the pre-deploy baseline.

The ACT half is unaffected: `--job-id` resolves through the cycle-scoped path and
verifies end-to-end on a real wedged production row (`decision: "eligible"`), and
the release signal's runnable command already carries `--job-id`. So the operator
is not stranded — but the surface whose entire purpose was "the operator can find
the row" is dead at production scale, which is the fourth surface named by PR
#1802's own review-failure retro invariant:

> every mechanism must have a verified path from its intended invoker to its effect

It had a verified path only on small fixtures. Its sole test
(`tests/test_file_orchestration_journal.py:14291`) asserts `wedged_count == 1` on
a one-row journal; `query_released_identity_blocked_jobs` has **zero** direct test
references anywhere in `tests/`.

## What changes

`query_released_identity_blocked_jobs` stops replaying the whole tree. It
enumerates candidate cycle scopes first and then does one cycle-scoped replay per
scope, reusing `_iter_pipeline_job_records_for_cycle`
(`file_orchestration_journal.py:5654`) — the memoized, per-cycle-invalidated
entrypoint that already exists.

This is not a new design. `#1734` already established the principle, and it is
stated verbatim in this repo's own test at
`tests/test_file_orchestration_journal.py:6749-6750`:

> the aggregate record budget guards the WHOLE-TREE replay, and a derivable cycle
> id no longer reaches it

The new query bypassed a rule already landed in the same file.

Explicitly rejected: **raising the budget.** `journal/` is append-only; a larger
cap moves the cliff instead of removing it, and it would be the second time this
mechanism is signed off without a production-scale oracle.

## Impact

- `services/orchestrator/file_orchestration_journal.py` —
  `query_released_identity_blocked_jobs` only.
- `tests/test_file_orchestration_journal.py` — new production-scale regression
  pin plus a residue-path pin.
- No CLI change: `services/orchestrator/cli.py` calls the same method name with
  the same return shape.
- No behaviour change for `--job-id`, for the recovery API, for either
  auto-retry door, or for any scheduler pass.

## Non-goals

- Changing what counts as a released identity-blocked row. The filter predicate
  is carried over unchanged.
- Touching the evidence-size guard (`resource_limit_blocked` /
  `MAX_EVIDENCE_BYTES`) — a different limit, tracked separately.
- Retention or archival of `journal/` history (that is #1757).
