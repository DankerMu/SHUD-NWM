Fixture level: expanded
Repair intensity: high
Change surface: file-journal retention inspection/locking seam; node-22 operator command, env, systemd, tests, and runbook.
Must preserve: cycle replay/merge and source-case behavior; live/released reservation and rollback recovery; all direct, reconcile, lock, and state-index authority.
Seams under test: typed cycle inspection seam; retention `run_retention`/CLI; archive manifest and restore verification; existing public cycle query.
Non-goals: production enforcement activation; state-index/direct-record pruning; transparent archive reads; scheduler-wide idle gate.

## 1. Journal Retention Contract

- [x] 1.1 Add a narrow file-journal owner seam that non-blockingly locks and canonically inspects one `(source_id, cycle_time)`, returns recognized members, and classifies existing rollback/reconcile plus released-identity-blocked rows without duplicating predicates.
- [x] 1.2 Add deterministic tests for source casing, continuation and event segments, live/released/incomplete-projection rows, lock contention, malformed/symlink/non-regular/unrecognized members, and cache/query behavior after mutation.

## 2. Archive Command

- [x] 2.1 Implement the default-disabled/default-dry-run node-22 command with strict root/config validation, 90-day cycle-name cutoff, scheduler-window inequality, fresh-frontier fail-closed behavior, and candidate union from exactly `latest/`, `journal/`, and `pipeline-events/` under one aggregate `MAX_FILE_JOURNAL_DISCOVERED_FILES`/`MAX_FILE_JOURNAL_SCAN_DEPTH` budget; prove a boundary-sized valid set is complete and one-over-budget enforcement removes nothing.
- [x] 2.2 Implement one-cycle `tar.zst` creation, per-member and archive SHA-256 manifest, temporary verification and atomic no-clobber publication, exact manifest-bound unlink, idempotent partial-cleanup retry, conflict refusal, and bounded receipts.
- [x] 2.3 Add requirement-driven tests for dry-run, eligible enforce, active/live/malformed/busy blockers, frontier/config failures, archive conflict/partial cleanup, unchanged sibling authority/state-index, and archive/restore query parity. Prove new-behavior tests fail against pre-change source in one batched red run and leave no `red-proof` stash.

## 3. Deployment and Recovery

- [x] 3.1 Add `NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED`, `NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN`, `NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS`, and `NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT` to `infra/env/compute.scheduler-dbfree.env.example` (and the generic `infra/env/compute.example` only when that template owns the same deployment surface); add a disabled-by-default node-22 user oneshot/timer that loads that EnvironmentFile, uses the pinned active interpreter, runs daily at 04:45 UTC with randomized delay and timeout, and has no scheduler-wide idle condition.
- [x] 3.2 Update the current-production runbook with install, dry-run inspection, explicit later activation, receipt interpretation, failure handling, and contained no-clobber restore/query-parity drill.

## 4. Evidence Floor

- [x] 4.1 Run `uv run ruff check scripts/node22_scheduler_journal_retention.py services/orchestrator/file_orchestration_journal.py tests/test_scheduler_journal_retention.py tests/test_file_orchestration_journal.py tests/test_state_manager_generation_history.py` with zero findings.
- [x] 4.2 Run `uv run pytest -q tests/test_scheduler_journal_retention.py tests/test_file_orchestration_journal.py tests/test_retention_frontier.py tests/test_state_manager_generation_history.py` with all scenarios passing.
- [x] 4.3 Run `openspec validate archive-file-journal-cycle-retention --strict --no-interactive` successfully.
- [ ] 4.4 On node-22, without `uv sync` or database access, run the checked-in script with `/scratch/frd_muziyao/NWM/.venv/bin/python` against the real journal in dry-run mode; retain a receipt proving zero hot mutation, current frontier/window inputs, candidate counts, and live/active exemptions.
- [x] 4.5 On node-22, use an isolated copy of one eligible production-shaped cycle to run enforce plus restore; retain manifest/archive/member digest evidence, pre/post cycle-query parity, and checksums proving `pipeline-jobs/` and `index-last.json` are unchanged. Do not enable the production timer or enforce against the live journal in this PR.

## 5. Review and Completion

- [ ] 5.1 Complete high-risk cross-review of file/path safety, concurrent writer exclusion, canonical live-row preservation, archive idempotency/recovery, evidence coverage, and #1775 history-anchor compatibility; close every blocking finding.
- [ ] 5.2 Confirm every acceptance scenario and Evidence Floor item on the frozen final head, with no weakened tests/specs and no untracked `red-proof` stash.
