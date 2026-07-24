## 1. Scheduler Retry And Artifact Guards

- [x] 1.1 Preserve DB-free runtime flags in automatic scheduler retry manifests
  for node-22 forecast and forcing retries.
  Evidence floor: focused test shows an automatic DB-free retry manifest
  contains `scheduler_db_free_required=true`, file backend identity fields,
  prior job id, new Slurm/job identity, stage/model/source/cycle identity, and
  runtime env that allows SHUD forecast execution without `DATABASE_URL`.
- [x] 1.2 Preserve DB-free runtime flags in manual/operator retry manifests.
  Evidence floor: focused test shows manual retry carries the same DB-free file
  backend fields as automatic retry, records prior job id/new Slurm id/stage/
  model/source/cycle/retry attempt, and does not require `DATABASE_URL`.
- [x] 1.3 Add missing forcing/copyback source pre-resume classification.
  Evidence floor: a historical successful forcing/forecast record with missing
  `forcing_package_uri` tree blocks before downstream forecast submission with a
  stable artifact/copyback code and no generic `NODE_FAILURE`.
- [x] 1.4 Add an explicit exact-cycle missing-forcing recovery policy without
  weakening the default blocker.
  Evidence floor: focused tests cover default block, plan preview, authorized
  missing-URI and missing-path recovery, non-direct-grid rejection, raw
  manifest absent/identity-mismatch rejection, malformed/unbounded operator
  use, preserved warm lineage with no cold fallback, and an 18-member forcing
  cohort routed through `produce_forcing_array` under the global bound of 32.

## 2. Reconcile And Terminal-State Idempotency

- [x] 2.1 Bind Slurm reconcile success to manifest/task identity.
  Evidence floor: tests cover matching submitted manifest/task/stdout identity
  -> success, and generic job-name-only terminal status -> unverified.
- [x] 2.2 Preserve legacy/non-DB-free reconcile compatibility or document a
  precise non-goal.
  Evidence floor: test covers old persisted manifest/stdout identity
  reconstruction or an explicit compatibility smoke proving non-DB-free/facade
  callers still behave as before.
- [x] 2.3 Treat completed cycle/stage/copyback evidence as terminal despite
  stale hydro `created` rows.
  Evidence floor: scheduler selection skips the candidate and records terminal
  evidence without submitting Slurm work.

## 3. Bounded Concurrent Scheduler Operation

- [x] 3.1 Add bounded live-pass progress guard and lock release evidence.
  Evidence floor: test simulates no progress under a live pass and verifies a
  stable blocker/exit, bounded evidence, and no retained production lock.
- [x] 3.2 Verify concurrent submit behavior locally.
  Evidence floor: scheduler tests cover `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND`
  greater than `1` without duplicate retry/resubmit for the same candidate.
- [x] 3.3 Move basin forcing concurrency to globally bounded Slurm arrays.
  Evidence floor: scheduler tests allocate the current 14/18/4 cohort sizes to
  14/14/4 under a global bound of 32; chain tests preserve the shared cohort
  budget; Gateway tests render `%N`, clamp against the resource profile/task
  count, and reject malformed budgets before `sbatch`.

## 4. Documentation, Validation, And Node-22 Deployment

- [x] 4.1 Update operational runbook/issue evidence for the safe artifact
  restoration and latest-code service restart path.
- [x] 4.2 Run local verification:
  `uv run pytest -q <focused scheduler/retry/reconcile tests>`;
  `uv run ruff check .`;
  `openspec validate fix-node22-scheduler-business-concurrency --strict --no-interactive`.
- [x] 4.3 Run risk-adaptive review and final gap sweep on the implementation.
- [x] 4.4 Push, open PR, wait for required CI, and post evidence bundle.
- [x] 4.5 On node-22, pull merged/latest code, remove one-at-a-time emergency
  override, restart `nhms-compute-scheduler.service`/timer with concurrent bound
  greater than `1`, and capture live business evidence with at least two
  eligible candidates or array tasks: service env, pass id, candidate/task
  identities, Slurm submissions/reconcile terminal evidence, duplicate-free file
  journal progress, and lock release. An explicit no-work pass is safe-state
  evidence only and blocks issue completion until a business-work receipt exists.
- [x] 4.6 Document the exact `--repair-missing-forcing --plan` and `--submit`
  operator commands, the raw/direct-grid/warm-state preconditions, and the
  evidence fields that prove forcing ran as a Slurm array rather than on the
  login node.
