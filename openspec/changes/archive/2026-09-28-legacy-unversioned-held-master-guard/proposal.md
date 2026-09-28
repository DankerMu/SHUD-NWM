## Why

#2668 left held shape (c) without an exit. Shape (c) is a `reserved` forecast cohort master (`stage` in `FORECAST_COHORT_STAGE_ALIASES`, or `job_type` when `stage` is empty — reconcile's rule; non-empty `cohort_members`) with no `accepted_submit_contract_version`. Restart reconcile reports it `legacy_unversioned_read_only` every pass. Demote refuses it, and `bind-reserved-job` refuses it as `legacy_unversioned_unsupported`. After #2667 such a row freezes its source's forward lane (issue #2674).

Writer trace (read-only, master `98a770121`):

- **No current writer mints (c).** The only writer of fresh forecast cohort reservations is `_reserve_cycle_stage` (`chain_forecast_orchestrator_cycle.py:716`), and it always stamps the contract version.
- **Auto retry.** For a current master it returns a virtual `pending` namespace. Only a legacy predecessor gets a clone that pops the version, and that clone is written `pending` with a null key.
- **Manual retry.** The clone is `pending` with key `manual_retry:*`. No current writer can move it to `reserved`.
- **Auto-retry clone of a legacy master.** The file-lane clone keeps `stage` with a null key but drops `cohort_members` (field whitelist). A non-versioned reclaim request would backfill the members from the request and write it `reserved`, unversioned. No current caller sends that request.
- **The persistence API does not enforce the invariant.** `reserve_pipeline_job`, `upsert_pipeline_job` (on an existing legacy row) and `append_historical_pipeline_job` accept a (c) row, and `reclaim_pipeline_job_reservation` recycles a dead legacy forecast master back to `reserved` unversioned (`row = dict(existing)`). So (c) is reachable, but only from a pre-existing legacy seed:
  - the historical import `import_historical_scheduler_state`;
  - a dead legacy master that gets reclaimed.
- **Production seed count.** The node-22 production journal holds **0** unversioned forecast cohort masters in any status, and 0 rows of shape (c). This comes from a read-only raw scan on 2026-09-28 with a predicate aligned to reconcile's (see design Context).

Deleting the reconcile branch is not safe. A (c) row would then fall through to the generic exact-comment lane. That lane queries with an empty expected user and no contract version or attempt anchor, and it takes the first record without an identity match. The result would be an unverified bind through an untrusted query, for a cohort array the lane cannot represent. So the fix closes the writers and keeps the branch.

## What Changes

- **Writer invariant.** No journal writer SHALL persist a `pipeline_job` row that is shape (c). Every public writer refuses such a result with one named error and writes zero bytes:
  - `upsert_pipeline_job`
  - `reserve_pipeline_job`
  - `reclaim_pipeline_job_reservation`
  - `append_historical_pipeline_job`. The historical import additionally pre-scans its job rows and fails closed before creating the root or writing any byte.
  - any other `pipeline_job` append path that the implementer finds
- **Caller pin.** The version stamp on `_reserve_cycle_stage` is pinned by a test.
- **Kept as defense in depth, unchanged:**
  - the reconcile `legacy_unversioned_read_only` branch;
  - the `bind-reserved-job` refusal `legacy_unversioned_unsupported`;
  - the listing `escalate` entry.

  They only matter for a row that predates this change, and production has none.
- **Runbooks.** Shape (c) moves from "no exit yet, #2674" to "unreachable from current writers since #2674; a pre-existing legacy row is escalated, never hand-edited".

## Triage

```text
Issue type: bugfix (prevention)
Fixture level: expanded
Upstream suggested level: absent (issue: S "prove + delete" / M "exit"); override: the proof finds (c) reachable only from legacy seeds, so the fix is a writer guard, not a deletion; the guard sits on the persisted-state writer path.
Blast radius: a guard too broad refuses a legitimate forecast write and stalls the chain; too narrow leaves a writer that still mints (c).
Selected risk packs: file IO / persisted state, legacy compatibility, error handling / zero-byte refusal, schema (new error code), documentation.
Evidence floor: per-writer refusal tests (red before, green after), legitimate writes unchanged (existing journal/reconcile/retry suites green), caller pin at :716, node-27 focused and full suites.
```

## Deviation from the issue text

The issue says to delete the dead branch and its refusal code once (c) is proven unreachable. (c) is not unreachable from legacy seeds, and deleting the branch routes a seed row into the generic exact-comment lane, where it could be bound without verification through an untrusted query (design Decision 4). So the branch, the refusal code and the listing entry stay as fail-closed defense in depth, and the writers are what get closed.

## Impact

- `services/orchestrator/file_orchestration_journal.py`: the writer guard.
- `services/orchestrator/file_orchestration_migration.py`: pre-scan of job rows before the journal root is created (design Decision 3).
- Tests that seed (c) rows (reconcile, bind, listing) move to a test-only raw-record seed that simulates pre-existing data.
- `docs/runbooks/failed-basin-retry.md`, `docs/runbooks/scheduler-dbfree-typed-reasons.md`.
