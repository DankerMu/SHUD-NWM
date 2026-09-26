## Context

This batch has three independent, small defects. Each issue body already contains a measured root cause and a recommended fix, and this design adopts those recommendations. The pull request is a single PR, but each fix is contained within its own files.

## Decisions

### D1 — #1922: fd ownership in both owners (no helper merge)

Each copy gets the same shape: `fd = os.open(...)`, then a `try` body that runs the existing checks and ends in `return fd`. On any exception after the open, `os.close(fd)` runs exactly once. It sits inside its own `try/except OSError: pass`, so a close failure can never mask the original error. The original error is then re-raised unchanged (`ProductionObjectStoreValidationError`) or wrapped exactly as today (`OSError` / `SafeFilesystemError` → `PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE` with the same message). A failure in `os.open()` itself has no fd to close, and its existing wrap is unchanged.

The two copies stay byte-identical, and a test asserts that. They are not merged into a shared leaf, which the issue marks as out of scope. The success path still returns a live fd that the caller closes in its existing `finally`, so nothing is closed twice.

### D2 — #2478: saturation is the asserted reason; wall clock is only a safety net

- For `boundary in ("byte", "row")`, set `COMMENT_SACCT_TIMEOUT_SECONDS = 30.0`. Saturation finishes in milliseconds. The #2107 "do not extend the timeout" constraint protects only the `wall_time` leg, whose oracle is the timeout itself.
- Assert the reason. `caplog`, at WARNING on the `services.orchestrator.reconcile` logger, must contain `sacct query exceeded bounded output (bytes)` for `byte` and `(rows)` for `row`, and must NOT contain `sacct query timed out`.
- Keep `terminated_path.exists()`: the saturation path's program order guarantees that the trap was installed. Keep the `durable_write_count == 0`, the unchanged cohort and job, and the reap checks.
- **Order.** The reason assertion goes right after the `action == "query_unavailable"` check and before the marker assertion. A fake that times out never writes the marker, so the marker assertion would otherwise fire first and hide the reason.
- **Red proof:** temporarily make the fake sleep before its output (a forced timeout). The byte and row legs must fail on the reason assertion. Record the failing output, then restore.
- **Rejected:** accepting a negative returncode as termination. That path proves nothing (see the issue).

### D3 — #2476: never-running is BLOCKED after cleanup

After the DELETE succeeds with `cancelled`, `if not reached_active: raise GatewayProofBlocked(f"long smoke job {job_id} never reached running within {CANCEL_WAIT_MAX_ATTEMPTS} polls (last {last_status!r}); cancelled for cleanup, cancel-while-active not proven")`.

The DELETE is still sent first, so that the 600 s smoke job does not hold the partition. The existing catch at `:442-445` turns the exception into stage `BLOCKED`, top-level `BLOCKED`, `live_proof_accepted=false` and a non-empty `dependency_blocker`.

The docstring at `:11-12` changes to "must reach RUNNING". The PASS path keeps `cancelled_while_active: True` in its counts. A test mode in `_FakeClient` returns `pending` for long jobs; the test asserts BLOCKED, the blocker text containing `'pending'`, and that the DELETE was issued.

## Risks / Trade-offs

- **#2476:** a busy partition now produces BLOCKED where it used to produce a false PASS. That is the intended honest result, and the blocker text says why. Making the wait configurable is a possible later mitigation and is not in scope.
- **#2478:** the 30 s ceiling lengthens the worst-case duration of a failing byte or row leg. A passing leg is still milliseconds.
- **#2478, residual flake source.** Program order guarantees that the trap is installed, but not that the marker gets written. After saturation, `_terminate_and_reap` (`services/orchestrator/reconcile.py:1151-1165`) sends SIGTERM, waits 1 s, then sends SIGKILL. Under extreme starvation the handler might not run within that 1 s. The signature would be: `saturated` warning present, marker missing, `returncode == -9`.
  - If the ≥100 load run shows it, stop and escalate.
  - `reconcile.py` is out of scope.
  - Accepting `rc < 0`, retries and weakening the marker check are all forbidden.
- **Spec home for #2478.** No spec owns the sacct-bounds oracle. `gateway-reconcile-test-partitioning` owns this test file's completeness, so the new requirement lives there. Its partition-time "without changed assertions" scenario is about moves between files, not this deliberate oracle change.
