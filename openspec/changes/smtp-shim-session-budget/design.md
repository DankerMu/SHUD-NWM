# Design: smtp-shim-session-budget

## Change surface

`scripts/node27_frontier_smtp_sendmail.py` @ origin/master (328 lines;
issue cites d9276a1b coordinates — 8BITMIME work shifted them, current
verified): `SMTP_TIMEOUT_SEC` :45; `default_smtp_factory` :68-79
(timeout → socket-level per-op); `_run` :242-294 (stage ladder: connect
:252-258, ehlo :261-268, login :269-270, send :271-283; teardown
`finally: smtp.quit()` :290-294 with blanket except); `main` :297-312
(containment: _UsageError→64, Exception→70); `_entrypoint` :315-324.
Lane: `scripts/node27_frontier_stall_alert.py` `SENDMAIL_TIMEOUT_SEC =
60` :148; `subprocess.run(..., capture_output=True, timeout=...)`
:926-932; `except subprocess.TimeoutExpired` :935-937 (error.stderr
populated by capture_output on POSIX — probe-verified, may be None
when the child wrote nothing — but unread today); failed send not recorded
:1313-1327; receipt `send_failures`. Runbook
`docs/runbooks/current-production-ops.md:2108-2111` ("nested" wording;
fixture-review P1-1 corrected — issue's :2033-2036 is unrelated §10.4
text; exit-code table :2103-2105 gets the new reason token).
Tests: `tests/test_node27_frontier_smtp_sendmail.py` (34 test
functions / 46 collected items via 4 parametrize sites, factory seam
`smtp_factory` injection; lane file collects 95), `tests/test_node27_frontier_stall_alert.py`.

Risk triage: compact fixture. The shim is a leaf CLI with an injectable
factory seam and full-class containment; the budget adds one
enforcement mechanism and one config knob. Highest risks: (1) signal
handling correctness (handler restoration, non-main-thread degradation,
alarm leaking into pytest), (2) teardown re-blocking past the wall
after budget expiry, (3) breaking the 34 existing tests' expectations
(success path evidence `SMTP-ACCEPTED` unchanged), (4) budget exception
escaping into `main`'s generic handler as SMTP-INTERNAL-ERROR rc=70
instead of the structured stage line rc=69.

## Key decisions

1. **SIGALRM/`signal.setitimer` deadline, not stage-boundary checks**:
   the dribbling-peer acceptance ("仍在预算内退出，不依赖外部
   SIGKILL") is unmeetable by boundary checks alone — a mid-`send`
   dribble never returns to a boundary. A one-shot
   `setitimer(ITIMER_REAL, budget)` whose HANDLER first sets a
   `budget_tripped` flag and then raises a private
   `_SessionBudgetExceeded(Exception)` (NOT an OSError subclass — the
   existing `except OSError` arms must not swallow it) interrupts the
   blocked socket op in-band (probe-verified through ssl-wrapped
   reads). Exception-handling STRUCTURE (fixture-review P1-2 — the
   naive sibling-arm placement leaves the post-send region :284-289
   and the disarm window uncovered, escaping to main as rc=70):
   (a) the budget `except` arm sits OUTERMOST in `_run`, covering the
   factory call, the whole stage ladder, the post-send region AND the
   teardown finally; (b) `main` carries a backstop
   `except _SessionBudgetExceeded` emitting the same structured line +
   69, so no microsecond window can surface as SMTP-INTERNAL-ERROR;
   (c) post-250 rule — a `wire_outcome` variable is None until
   `send_message` returns and holds the refused dict after; budget
   expiry with `wire_outcome` bound proceeds with normal post-send
   reporting (a fired alarm must not unsay a received 250, consistent
   with the :293 invariant). Structured line:
   `SMTP-FAILED stage={stage} host={host} reason=session-budget
   elapsed={...}s budget={...}s` → EXIT_UNAVAILABLE (69). The current
   `stage` variable is the attribution — no new bookkeeping.
2. **Budget value and alignment**: `SESSION_BUDGET_SEC = 45.0` module
   constant; env override `NHMS_SMTP_SESSION_BUDGET_SEC` (validated
   like NHMS_SMTP_PORT: positive float, usage-error otherwise).
   Alignment is guarded by a TEST importing both modules:
   `SESSION_BUDGET_SEC < stall_alert.SENDMAIL_TIMEOUT_SEC` with margin
   (assert 45 <= 60 - 10 or equivalent) — either constant drifting
   alone goes red. Comment at the constant names the lane constant.
3. **Signal lifecycle**: install handler + `setitimer` AFTER config/
   parse (usage errors need no alarm), immediately before the factory
   call; in a `finally`, disarm with the race-safe sequence
   `setitimer(0)` → set SIG_IGN → restore previous handler
   (fixture-review probe: restoring SIG_DFL while the timer expires
   concurrently kills the process with exit 142 and ZERO stderr —
   total evidence loss; the SIG_IGN interposition closes it). pytest
   process must be left pristine. Non-main-thread (`signal.signal`
   raises ValueError): degrade gracefully — proceed without alarm
   (per-op min() still applies), silent (a stderr line would pollute
   the lane protocol); note in docstring.
4. **Teardown after expiry**: the teardown finally reads the
   `budget_tripped` FLAG (not the exception — with the outermost arm
   the except body runs after the finally) to choose `smtp.close()`
   (non-blocking, probe-verified: read-mode makefile close + sock
   close) over `quit()`; the alarm stays armed until AFTER teardown so
   a hanging `quit()` on the normal path is also bounded (the blanket
   `except Exception` in the teardown swallows a budget exception
   raised there — a 250 already printed stays printed).
5. **Per-op cap**: factory call becomes
   `smtp_factory(host, port, min(SMTP_TIMEOUT_SEC, remaining))`
   (remaining ≈ budget at the connect call). Stated honestly
   (fixture-review P2-2): at default values this evaluates to 30.0 —
   a no-op whose value is only for small env budgets; SIGALRM is the
   real enforcement. Keeps the F6 pin :547 green at defaults.
6. **Lane stderr fold**: `except subprocess.TimeoutExpired as error:`
   decodes `error.stderr` (bytes|None) and appends any non-empty
   tail (bounded, e.g. last 500 chars) to the SendResult error string.
   Pure observability; rc stays 124; no retry-semantics change.
7. **Runbook**: three-layer wording (per-op 30 s / session budget 45 s
   env-overridable / lane 60 s SIGKILL fallback) + rc=124-without-stage
   read-out (shim self-hang; message MAY be delivered; next tick
   resends by design).

## Must preserve

- All 34 existing shim tests green UNMODIFIED; `SMTP-ACCEPTED` success
  evidence chain byte-identical; exit-code contract (0/64/69/70)
  unchanged; existing SMTP-FAILED/CONFIG/INTERNAL line formats
  unchanged for non-budget paths.
- Lane D2 retry semantics untouched (:1313-1327); rc mapping (124/127)
  untouched.
- No new dependency; stdlib signal only.
- `main`'s never-raises contract: the budget exception is handled
  inside `_run` (or an explicit arm) — it must never surface as
  SMTP-INTERNAL-ERROR.

## Seams under test

Existing `smtp_factory` injection seam (34 tests use it). Budget tests
inject fakes whose methods sleep: (a) slow-per-op fake — each op
sleeps just under SMTP_TIMEOUT_SEC (scaled down via env override:
set NHMS_SMTP_SESSION_BUDGET_SEC to e.g. 0.5 and have ops sleep 0.3 —
NO real 45 s sleeps in tests); (b) dribbling send — one op sleeps far
past the budget in a single blocking call (simulates mid-op dribble;
SIGALRM must interrupt it). Lane test: fake TimeoutExpired with stderr
bytes → SendResult.error contains the shim line.

## Test plan (maps to acceptance)

1. Alignment guard: cross-module constant assertion with margin.
2. Slow-per-op fake + tiny env budget → exits rc=69 before wall,
   stderr has `SMTP-FAILED stage=` + `reason=session-budget`, wall
   clock of the test « 60 s.
3. Dribbling/mid-op-blocked fake → interrupted within budget, rc=69,
   stage attribution correct (stage=send when blocked in send).
4. Stage attribution per phase: budget expiry during login vs send
   yields the respective stage token.
5. Env override validation: bad value → SMTP-CONFIG-ERROR rc=64;
   absent → default 45.
6. Success path: generous budget → SMTP-ACCEPTED unchanged; alarm
   disarmed and handler restored after main() returns (assert
   signal.getsignal(SIGALRM) restored).
7. Teardown: budget expiry → smtp.close() called, quit() NOT called
   (fake records calls).
8. Lane: TimeoutExpired with populated stderr → folded into
   SendResult.error; without stderr → unchanged message.
9. Existing 34 shim tests + lane tests green unmodified.

## Risks to watch

- macOS vs Linux setitimer semantics are the same for ITIMER_REAL;
  tests must disarm reliably even on failure (finally).
- pytest-timeout or other plugins using SIGALRM would conflict — check
  installed plugins before relying on SIGALRM in tests (report if a
  conflict exists; none expected: plugins are zarr/asyncio/anyio).
- The factory seam receives the min() timeout — one existing test may
  pin the exact factory timeout argument (#1373 F6 pin): check
  tests for `SMTP_TIMEOUT_SEC` assertions and reconcile (the pin may
  need the enumerated-authorization treatment if it asserts the raw
  constant is passed through — flag to orchestrator if so, do not
  silently change).
