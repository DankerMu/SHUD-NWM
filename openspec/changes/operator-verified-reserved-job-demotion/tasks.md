## 1. Accepted-submit and journal authority

- [x] 1.1 Add `operator_verified_absence` to the accepted accounting decisions only; keep it out of generic versioned transitions, identity-streak decisions, and manual-retry source statuses, with negative tests for every forbidden path.
- [x] 1.2 Implement one file-journal typed CAS demotion API over current master identity, exact attempt/UTC anchor, held accounting tuple, status, and unbound fields; invalid input raises a typed error and any stale/mismatched durable state returns a zero-write refusal.
- [x] 1.3 On success, atomically append the `reservation_lost/operator_verified_absence` master (null reason class), same-attempt active hydro-member `failed/SLURM_RESERVATION_LOST` projections, and a validated audit event carrying bounded `checked_by`, normalized `checked_at`, verification note, expected attempt/anchor, and prior blocker.

## 2. Reclaim and scheduler integration

- [x] 2.1 Extend `reclaim_pipeline_job_reservation` to accept exactly the automatic and operator absence decisions while preserving every other predicate and the fresh lock-owned attempt anchor.
- [x] 2.2 Extend `_verified_accepted_submit_forecast_retry` by the same exact decision set while preserving its caller status/unbound gate, outcome/source/matched/cohort checks, and marker-free automatic-absence compatibility; pin `identity_mismatch_released` and other terminal sub-shapes as false.
- [x] 2.3 Drive the real held-row → operator demotion → cycle retry shortcut → reclaim path in tests, proving the current-master reclaim ignores lock-external proposed attempt/anchor values, derives attempt+1 and a fresh anchor from durable/locked state, preserves immutable cohort identity, emits one resubmission eligibility decision, and avoids the `PIPELINE_ALREADY_ACTIVE` short-circuit.

## 3. Operator CLI

- [x] 3.1 Add `nhms-pipeline demote-reserved-job` to click and argparse with required `--journal-root`, `--job-id`, `--expected-attempt`, timezone-aware `--expected-attempt-started-at`, `--checked-by`, timezone-aware `--checked-at`, bounded non-empty `--verification-note`, and non-interactive `--confirm`.
- [x] 3.2 Make both entrypoints emit identical sorted success JSON and map validation/CAS refusal to stderr plus exit 2; absence of `--confirm` must fail before repository construction or any write.
- [x] 3.3 Add or reuse a focused CLI test suite for both entrypoints and, if a new suite is created, add it to `ORCHESTRATOR_CLI_IMPORTER_TESTS` with selector meta-guard coverage so a `cli.py`-only change schedules it.

## 4. Adversarial and atomicity matrix

- [x] 4.1 Prove byte-identical zero-write refusal for missing/wrong job id, non-master row, statuses `running`/`submitted`/`pending`/`reservation_lost`/`submission_failed`, bound or matched Slurm id, wrong outcome/source/decision/reason, missing/wrong attempt, missing/wrong anchor, missing operator evidence, and repeated invocation.
- [x] 4.2 Prove concurrent bind/permit/release/demote/reclaim winners make the stale operator request fail; inject record/event validation or append faults and prove no master/member/event partial authority state becomes durable before commit. After a successful authority append, inject direct/latest projection faults and prove the typed receipt reports committed success with bounded warnings, attempts remaining independent projections, journal replay stays authoritative, and a repeated request appends nothing.
- [x] 4.3 Prove the #1116 comment-less reconcile path remains `reserved/accounting_unavailable/comment_accounting_unproven`, HTTP manual retry still rejects reserved, PostgreSQL behavior is unchanged, and automatic `absence_retry_permitted` still reclaims.

## 5. Documentation and local evidence

- [x] 5.1 Rewrite `docs/runbooks/failed-basin-retry.md` Disposition case 3 with the exact Slurm verification, persisted attempt/anchor lookup, command invocation, output/event checks, and next-pass reclaim/resubmit checks; remove the unsupported hand-edit/no-safe-mechanism text.
- [x] 5.2 Produce one batched red proof against pre-change production code for the new public-seam tests, restore production, leave no `red-proof` stash, and record any implementation deviation or state no deviations.
- [x] 5.3 Run focused pytest covering journal CAS/event/reclaim, cycle integration, both CLI entrypoints, #1116 preservation, and selector importer ownership; run tracked-Python Ruff, strict OpenSpec validation, and `git diff --check`.

## 6. Node-22 live receipt

- [ ] 6.1 On node-22, select or safely create one real file-journal `reserved/accounting_unavailable/comment_accounting_unproven` unbound master; record its exact job id, attempt, anchor, cohort identity, journal hashes, and precondition that the target is confirmed dead by name/time/user/account `sacct` plus `squeue` checks.
- [ ] 6.2 Run the confirmed operator command, capture success JSON and durable audit event, then run one scheduler pass and prove the held row no longer causes `PIPELINE_ALREADY_ACTIVE`, reclaim minted one fresh attempt/anchor, and exactly one cohort resubmission occurred.
- [ ] 6.3 Capture negative live evidence with a stale anchor or repeated command showing non-zero refusal and byte-identical journal state; record cleanup/rollback boundaries and ensure no unrelated live row was changed.

## 7. Review boundary

- [x] 7.1 Confirm no changes to the automatic comment-capability gate, manual-retry source statuses, PostgreSQL schema/reclaim, Slurm configuration, generic decision whitelist, or identity-release reclaim behavior.
- [ ] 7.2 Completion self-audit maps every issue acceptance criterion and selected risk pack to the final diff, local tests, durable audit evidence, or node-22 receipt; record all deviations and route any deferral.
