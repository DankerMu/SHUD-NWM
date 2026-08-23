## Why

A pipeline-job row that reaches the `identity_mismatch_released` /
`reservation_lost` shape is a **permanent, silent wedge**. Its cohort never runs
again and no operator action can recover it.

`file_orchestration_journal.py:3364-3366` promises otherwise:

> The released row is a deliberate non-reclaimable terminal: reclaim only accepts
> `absence_retry_permitted`, so this idempotency key is spent. **Liveness comes
> from the next attempt minting a retry-suffixed key.**

That liveness mechanism does not exist. Both minting sites —
`chain_forecast_orchestrator_cycle.py:218-233` and
`file_orchestration_journal.py:8268` and `:8334` — are gated on
`should_auto_retry` (directly or through the caller at `:8278-8279`),
which is false by construction for this shape: the release transition
deliberately withholds `error_code` (`file_orchestration_journal.py:3410-3413`),
so `retry.py:200` reads `UNKNOWN_FAILURE`, `retry.py:202` classifies it
non-retriable, and `retry.py:204` marks it permanent. The retry budget is
untouched (`retry_count=0`, limit 6) — this is a classification outcome, not
budget exhaustion.

Production evidence (node-22, 4487 `journal/pipeline-jobs/*.json` rows): exactly
4 rows have ever reached this shape, all after the branch was introduced by
`f14343bf`, and **all 4 have no successor `_retry_<n>` row**. The mechanism is
0/4. The two most recent (cycle `2026-08-07T12:00Z`, GFS `3e066f456290` and IFS
`9c372471f1c1`) cost 44 model-runs: their `cohort_members` have no pipeline-job
rows at all and nothing has touched them since `2026-08-22T13:08Z`.

The existing manual channel does not help: `retry.py:73-74` defines
`MANUAL_RETRY_SOURCE_STATUSES` without `reservation_lost`, so
`file_orchestration_journal.py:9213` never classifies it as a retry source, so
`failed_job` resolves to `None` and `:8621` raises `RetryNotFoundError`.

## What Changes

- Add an **operator-gated** recovery path that records a durable operator
  attestation **on the released row itself** and writes **no** successor
  pipeline-job row, **without** consulting `should_auto_retry` and **without**
  stamping any `error_code`. An additive disjunct at the consuming call site then
  lets an ordinary pass mint `_retry_<n>` on a **free** identity and submit it.
  (An earlier draft of this proposal specified pre-materializing the successor
  row via `_next_current_master_retry_identity`. That was implemented, found
  INERT and self-blocking, and replaced; the finding is recorded in design D4 and
  the abandoned mechanism is now explicitly forbidden by the spec delta.)
- Make the released terminal **announce itself**: the release write point emits a
  queryable operator-visible record carrying a searchable token, instead of
  freezing silently. The emission is best-effort with respect to the release —
  it may never turn an already-durable release into a raise — and its own failure
  leaves a durable trace rather than degrading to silence.
- Cover both **prior-state shapes** a released row can arrive in — a fresh
  reservation, and one re-seeded by `reclaim_pipeline_job_reservation` — which is
  what `tests/test_production_scheduler.py:48713` and `:48762` actually
  distinguish (`:48762`'s docstring: "the SECOND *reservation* write point").
  There is exactly **one** release write point
  (`release_identity_blocked_reservation`, `file_orchestration_journal.py:3346`,
  decision constructed at `:3417`), so the signal is emitted once, there.

## Non-Goals

- **No automatic retry for this shape.** `AccountingStoreFlags = (null)` on this
  cluster means absence can never be proven through the comment leg, so a
  released row means "a job may still be running and we cannot know". Automatic
  minting would reverse the deliberate safety decision recorded at
  `file_orchestration_journal.py:3410-3413` and re-open the duplicate-submission
  class that #1116 closed. A streak cap would bound how many duplicates, not
  whether.
- **No `error_code` stamping.** `tests/test_production_scheduler.py:48713` and
  `:48762` pin the null `error_code` and the false `should_auto_retry` verdict;
  both SHALL stay green unweakened.
- No change to the two reclaim doors, to `absence_retry_permitted` semantics, or
  to HPC/Slurm configuration.
- **No Slurm-side liveness or absence check.** The recovery API SHALL NOT try to
  determine whether the Slurm array is still running — on this cluster it cannot
  (see above). Invoking it is an **operator attestation**, not a proof. This is
  the single highest-risk fact in the design and is stated here rather than left
  implied.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `job-retry-mechanism`: add an operator-gated recovery path and a mandatory
  operator-visible signal for released identity-blocked reservation rows, while
  the existing "SHALL remain outside automatic retry classification" requirement
  stays intact and unweakened.

## Impact

- `services/orchestrator/file_orchestration_journal.py`
- `services/orchestrator/chain_forecast_orchestrator_cycle.py`
- `tests/test_production_scheduler.py`
- `tests/test_file_orchestration_journal.py`
- `openspec/changes/recover-released-identity-blocked-reservation/specs/job-retry-mechanism/spec.md`
