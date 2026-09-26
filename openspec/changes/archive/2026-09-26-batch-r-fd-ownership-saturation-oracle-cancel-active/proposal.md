## Triage

```text
Issue type: bug x2 (#1922 fd leak, #2476 false PASS) + test oracle (#2478 load-flaky wrong-reason assertion)
Fixture level: standard
Blast radius: #1922 production-closure validator helper (two byte-identical owners); #2476 an opt-in live-proof
  emitter script (not the production submit path); #2478 one test's byte/row legs only
Selected risk packs: Error handling (fd cleanup must not mask the fail-closed error; BLOCKED semantics),
  Resource limits (fd leak), Test oracle (saturation vs timeout), Legacy compatibility (error codes/messages,
  receipt schema unchanged)
Evidence floor: tasks.md Evidence Floor; all local (batch R: local verification is enough)
```

## Why

Batch R of the 10-batch serial run. Master is `74c0f5347` (after Q #2643/#2645).

- **#1922.** `_open_runtime_prefix_dir` owns an fd from `os.open()` onwards but hands it over only on `return fd`. The post-open `os.fstat()`, the `stat_no_follow()` containment/no-follow check and the explicit not-a-directory check can all raise, and each exit leaks the fd. There are two byte-identical copies:
  - `services/production_closure/object_store_validation_runtime.py:628-645`;
  - `services/production_closure/object_store_validation_path_safety.py:367-384`.

  The result is still fail-closed. A long-lived process, though, accumulates descriptors on repeated hostile or misconfigured requests.
- **#2478.** In `tests/test_gateway_reconcile_comment_sacct_bounds.py::test_real_sacct_process_bounds_reap_and_leave_inflight_cohort_unchanged`, the `byte` and `row` legs use a 2.0 s wall deadline (`:297-301`). Under CPU starvation the fake `sacct` shell never runs its first statement before that deadline. Production then correctly takes the timeout path, SIGTERM kills the shell before its trap exists, and the `terminated_path.exists()` assertion (`:334-335`) goes red.

  The deeper defect is the oracle. Timeout and saturation both surface as `query_unavailable`, so nothing proves that the byte or row bound was the reason.
- **#2476.** In `scripts/m24_gateway_proof.py::_run_submit_cancel_stage` (`:333-378`), a long job that never reached `running` within `CANCEL_WAIT_MAX_ATTEMPTS` polls is still cancelled, and the stage then returns `PASS`. That gives `live_proof_accepted=true` for a cancel-before-start. The spec (`slurm-gateway-node22-deployment`, "Long-job cancel receipt") requires cancel-while-active and forbids an unfalsifiable step.

## What Changes

- **#1922:** both owners keep the fd function-local until a successful return. One cleanup path closes it exactly once on every post-open exception, then re-raises or wraps the original error unchanged. A close failure never replaces that error.
- **#2478:** for the `byte` and `row` legs only:
  - raise their wall deadline to a safety net (30 s);
  - assert the saturation reason itself: the `sacct query exceeded bounded output (bytes|rows)` warning via `caplog`, or an equivalent spy on `ReconcileQuerySaturated.boundary`;
  - keep the marker assertion.

  A forced timeout (a fake that sleeps first) must make those legs fail. The `wall_time` leg and `services/orchestrator/reconcile.py` are unchanged.
- **#2476:**
  - After a successful cleanup DELETE, a stage whose long job never reached `running` raises `GatewayProofBlocked`, and the blocker text includes the last status. The stage and the receipt become `BLOCKED` with `live_proof_accepted=false`.
  - The module docstring says that RUNNING is required.
  - The receipt schema is unchanged: BLOCKED already means "not accepted".

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `production-object-store-migration`: the runtime-prefix directory open releases its descriptor on every rejection.
- `gateway-reconcile-test-partitioning`: the sacct byte/row bound legs prove saturation, not a wall-clock timeout.
- `slurm-gateway-node22-deployment`: the cancel stage is not PASS unless the long job was observed RUNNING.

## Impact

- **Code:**
  - `services/production_closure/object_store_validation_runtime.py` and `object_store_validation_path_safety.py` (the helper bodies only);
  - `scripts/m24_gateway_proof.py` (the cancel stage and its docstring).
- **Tests:**
  - `tests/test_production_object_store_validation.py`, or the facade-contract test (new fd-cleanup cases for both owners);
  - `tests/test_gateway_reconcile_comment_sacct_bounds.py` (the byte/row legs);
  - `tests/test_m24_gateway_proof.py` (a new never-running mode).
- **No change:** error codes and messages, the no-follow/containment semantics, the reconcile production contract, the `wall_time` leg, `services/m24_live/receipt.py`, and the gateway service.
