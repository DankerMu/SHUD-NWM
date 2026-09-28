# Design: legacy unversioned held master guard (#2674)

## Context

Shape (c) is a pipeline-job row with all four properties:

- a forecast cohort stage, by the same rule reconcile uses (`reconcile._is_forecast_cohort_job` / `accepted_submit_cohort.is_forecast_cohort_stage_name`): `stage` in `FORECAST_COHORT_STAGE_ALIASES`, or, when `stage` is empty, `job_type` in it;
- non-empty `cohort_members`;
- no current `accepted_submit_contract_version` (`accepted_submit_contract_is_current(row)` is false);
- `status == "reserved"`.

Read-only trace of the writer paths (master `98a770121`, cited by function name):

| writer | produces (c)? |
|---|---|
| `_reserve_cycle_stage` → `reserve_candidate` → `reserve_pipeline_job` | no: the version is stamped unconditionally in the forecast-cohort branch |
| `reserve_pipeline_job` | accepts an unversioned forecast cohort record; its clean-reservation gate runs only for current masters |
| `upsert_pipeline_job` on an existing legacy row | accepts `status="reserved"` (`normalize_accepted_submit_evidence` short-circuits for unversioned rows); no production caller passes `reserved` |
| `append_historical_pipeline_job` (used by `import_historical_scheduler_state`) | writes a legacy `reserved` row verbatim |
| `reclaim_pipeline_job_reservation` on a dead legacy forecast master | `row = dict(existing)` then `status="reserved"`, so it stays unversioned (the recycle door) |
| `permit_pipeline_job_retry` legacy branch | `reserved` → `reservation_lost` (the seed for reclaim, not (c) itself) |
| auto retry `schedule_auto_retry` | current master: virtual `pending`; legacy predecessor: `pending` clone with a null key and no version (the file lane's `_file_retry_job_record` is a field whitelist and drops `cohort_members`). A **non-versioned** `reclaim_pipeline_job_reservation` request matches that clone (unmatched branch: existing `pending` + null key), backfills `cohort_members` from the request, and writes it `reserved`, unversioned: **(c)**. No current caller sends that request, because `_reserve_cycle_stage` always sends a versioned one, and a versioned request against a non-current row returns `None`. |
| manual retry `_create_pending_manual_retry_job` | `pending` clone, key `manual_retry:*`; no writer moves it to `reserved` |
| demote, operator bind, identity release/recover | require the current version |
| `record_manual_repair`, repair scripts | no `pipeline_job` write |

Production: node-22 holds 0 rows of shape (c) and 0 unversioned forecast cohort masters in any status. The count comes from a read-only raw scan on 2026-09-28T05:21Z:

- Scope: every surface the store reads pipeline-job rows from, under `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`:
  - the cycle journal segments `journal/*/*.jsonl` (32 863 `pipeline_job` records, 18 583 job ids), taking the latest record per `job_id`;
  - the direct rows `pipeline-jobs/**/*.json`, including `by-cycle/` (19 726 files, 0 undecodable; the row is `payload` when present).
- Predicate: the rule above.
- Script: the one recorded in the PR.
- An earlier scan at 04:3xZ counted 33 750 records. The difference fits the journal-retention archive having moved expired cycles out of the live tree in between. Archived cycles are not read by the store.

Other `pipeline_job` appends that do not go through `_write_pipeline_job_unlocked` build their records themselves and call `_append_journal_records_unlocked`. None of them can produce (c) today:

- `reject_pipeline_job_submit_attempt` writes `submission_failed`.
- `mark_pipeline_job_permanently_failed` writes `permanently_failed`.
- `permit_pipeline_job_retry` and `demote_operator_verified_reserved_job` write `reservation_lost`.
- `project_forecast_cohort_tasks` and `_write_operator_bind_unlocked` require a current master.

Today, then, (c) is reachable only from a legacy seed: the historical import, a reclaim of a dead legacy master, or a non-versioned reclaim of an auto-retry clone of a legacy master. Each path depends on caller discipline and none is refused at the writer.

## Decisions

1. **One predicate, enforced at the durable pipeline-job write.**
   - Add `_is_legacy_unversioned_reserved_forecast_master(row)`, true exactly for shape (c).
   - Every path that appends a `pipeline_job` record checks the row it is about to persist and refuses shape (c) before any byte is written: `upsert`, `reserve`, `reclaim`, `append_historical`, the retry clones, and any other append the implementer finds.
   - The refusal raises `FileOrchestrationJournalError("file_journal_legacy_unversioned_reserved_forecast_master", field="accepted_submit_contract_version")`.
   - The check sits at the **entry of `_write_pipeline_job_unlocked`**. It runs before the conflict check, before the sequence allocation, and before `_sync_reconcile_inventory_for_row_unlocked`, which writes or rewrites a reconcile-inventory anchor before the journal append. So a refused write publishes no anchor and rewrites no existing one.
   - It also sits at the **record-level append funnels** (`_append_journal_record_unlocked` / `_append_journal_records_unlocked`), as a backstop for the batch appenders listed above.
   - If the implementer finds a record path below or beside these, it gets the same check. The PR lists every append site and names the funnel.
   - It is not a per-caller check that a future writer could miss.
   - Derived projection rewrites (latest/direct restore of an already-persisted row) are not new durable records and are not refused. A pre-existing row stays readable.
2. **Reclaim of a dead legacy forecast master is refused.**
   - The recycle door closes with the same error.
   - The check is on the **existing** row's shape, not only on the row about to be written: `reclaim_pipeline_job_reservation` raises when the persisted row is an unversioned forecast cohort master with non-empty `cohort_members` in any status (`_is_legacy_unversioned_forecast_cohort_master`, the status-free part of the predicate), once every `None`-returning gate has passed, before the row is built. The write-entry check alone would miss a non-versioned request without `cohort_members`: the backfill writes `[]`, clears the members and commits a `reserved` member-less unversioned forecast master, which reconcile routes to the generic exact-comment lane.
   - No current caller reclaims a legacy forecast master: the only forecast reclaim request comes from `_reserve_cycle_stage`, which is versioned, and a versioned request against a non-current-master existing row already returns `None` (no error) at the versioned gates, which run before the existing-row check (E4b).
   - The guard therefore changes no production behavior. It makes the invariant structural.
   - A stamping upgrade of the legacy row is deliberately not done: it would mint accepted-submit authority for a row whose identity was never proven (the #1805 concern).
3. **The historical import fails closed before writing anything.**
   - `import_historical_scheduler_state` appends cycles, then runs, then jobs, one row at a time. A raise part-way through would leave a partial import behind.
   - So the import pre-scans the normalized, non-skipped job rows with the same predicate. It runs right after the row-limit gate and before the journal root is created or verified, the #1955 D2 placement.
   - Any (c) row fails the whole import with the named error: no journal root is created and zero bytes are written.
   - A skip reason is deliberately not used. Skipping a held row would drop the only evidence that its Slurm job may be running, and a later pass could reserve the same work fresh.
   - The writer guard still backs `append_historical_pipeline_job` for any other caller.
4. **Defense in depth stays.** Deleting the reconcile branch would route a pre-existing (c) row into the generic exact-comment lane. There it would be queried with:

   - `expected_user=""`, with no contract version and no attempt anchor (`reconcile.py`, the generic `_query_comment_accounting_proof` call);
   - its first record taken as `proof.records[0]`, without `_reserved_record_identity_matches`.

   That is an unverified bind through an untrusted query, for a cohort array whose identity that lane cannot represent. So these remain unchanged:
   - the reconcile branch `legacy_unversioned_read_only`;
   - the bind refusal `legacy_unversioned_unsupported`;
   - the listing entry (`escalate`, `follow_up_issue` `#2674`).

   They cover a row that predates this change. The listing entry's follow-up reference stays `#2674`, because this issue documents the escalation. The runbooks say the shape is unreachable from current writers, and that a pre-existing row is escalated and never hand-edited.
5. **Tests seed (c) as pre-existing data.**
   - Suites that need a (c) row (reconcile `test_gateway_reconcile_comment_capability.py` / `test_gateway_reconcile_comment_accounting.py`, bind CAS, listing) seed it through one test-only helper.
   - The helper simulates a row written before this change. It disables the predicate only for the seeding call, by monkeypatching the module-level predicate to return false, and writes through the normal writer.
   - This adds no production seed parameter.
   - Where a test used `upsert` / `append_historical` / `reserve` to build (c), that call now raises, which is the point of the change. The test moves to the helper.

## Non-goals

- No new operator exit for (c).
- No deletion of the reconcile branch.
- No change for forcing-lane rows (#2675) or for non-forecast stages.
- No change to rows that are not `reserved` (legacy `pending` / terminal / `reservation_lost` rows keep being written as today).

## Mapping to the issue's acceptance criteria

- After this change (c) cannot be produced by any writer. The guard makes that structural. So the issue's "cannot be produced" branch applies going forward:
  - AC1: the writer trace is filed, in this design and the PR.
  - AC4: the invariant is pinned per writer, plus the caller pin.
  - AC5: the node-22 count is recorded (0).
- **Deviation from AC4:** the dead branch and refusal code are not deleted (Decision 4).
- The "can be produced" branch (AC2 exit reproduction, AC3 negative CAS tests) is intentionally not delivered: no writer can mint the row, and production holds none. A (c) row can therefore only be pre-change data, and it is escalated. The per-writer refusal tests take the place of AC3 for the only door that remains, which is the write itself.

## Required evidence

| id | scenario | expected |
|---|---|---|
| E1 | `reserve_pipeline_job` with a (c) record | named error, zero bytes: the journal tree hash is unchanged, including `reconcile-inventory/` (no anchor written) |
| E1b | A refused write through upsert, historical append, clone reclaim and an in-place rewrite, with the conflict check, sequence allocation and anchor sync patched to fail | named error; none of them runs (entry placement). Clone reclaim is an entry-placement case: the clone is member-less, so the reclaim's existing-row shape check passes and only the write-entry check refuses the backfilled row. The existing-row check is pinned by E4's member-less request instead |
| E2 | `upsert_pipeline_job` moving an existing legacy forecast master to `reserved` | named error, zero bytes |
| E3 | `append_historical_pipeline_job` with a (c) row | named error, zero bytes |
| E4 | `reclaim_pipeline_job_reservation` on a dead legacy forecast master (seeded through `permit_pipeline_job_retry`'s legacy branch or the helper), with and without `cohort_members` in the request | named error, zero bytes; a pre-existing reconcile-inventory anchor is byte-identical (not rewritten) |
| E4b | A versioned `reclaim_pipeline_job_reservation` request (current marker, `expected_submission_attempt`, `expected_submission_attempt_started_at`) on the same dead legacy forecast master, directly and through `_reserve_cycle_stage` -> `reserve_candidate` | `None` / `created=False`, no error, zero bytes. The versioned gates run before the existing-row shape check; moving that check above the first `None` gate fails both |
| E5 | Non-versioned `reclaim_pipeline_job_reservation` on the auto-retry clone of a legacy forecast master (`schedule_auto_retry` legacy branch: `pending`, null key, `cohort_members` empty, backfilled from the request) | named error, zero bytes |
| E6 | A (c) row whose `stage` is empty and whose `job_type` is `forecast`, through any guarded writer | named error, zero bytes |
| E7 | `import_historical_scheduler_state` over a snapshot containing a (c) row among valid cycles, runs and jobs | named error; no journal root created (or, for an existing root, zero bytes); nothing imported |
| E8 | Bypass appenders (`reject_pipeline_job_submit_attempt`, `mark_pipeline_job_permanently_failed`, `permit_pipeline_job_retry`, `demote`, `project_forecast_cohort_tasks`, operator bind) | behavior unchanged; covered by the existing suites staying green |
| E9 | Same writers with a versioned forecast master, a legacy forecast row in a non-`reserved` status, a forcing / non-forecast `reserved` legacy row, or a forecast row without `cohort_members` | written exactly as before |
| E10 | `_reserve_cycle_stage` for every forecast cohort alias | persisted row carries the current version (caller pin) |
| E11 | Pre-existing (c) row seeded by the helper | reconcile still reports `legacy_unversioned_read_only`; bind still refuses `legacy_unversioned_unsupported`; listing still `escalate` |
| E12 | Existing journal / retry / reconcile / scheduler suites | green, unchanged except the (c) seeding moved to the helper |

New-behavior tests are red before the guard and green after.

## Rollout

Merge, then node-22 `git pull --ff-only`. No timer stop. Production has no (c) row, so no live row is affected. The guard only refuses writes that no current caller performs.
