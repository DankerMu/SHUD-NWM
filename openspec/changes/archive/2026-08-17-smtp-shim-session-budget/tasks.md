# Tasks: smtp-shim-session-budget

## 1. Shim (scripts/node27_frontier_smtp_sendmail.py)

- [x] 1.1 `SESSION_BUDGET_SEC = 45.0` + env override
      `NHMS_SMTP_SESSION_BUDGET_SEC` (validated; bad value →
      _UsageError → rc=64); constant comment names
      `SENDMAIL_TIMEOUT_SEC` as the alignment partner.
- [x] 1.2 SIGALRM/`setitimer` one-shot deadline; the HANDLER sets a
      `budget_tripped` flag THEN raises private `_SessionBudgetExceeded`
      (NOT an OSError subclass); armed after parse/config, before the
      factory call; non-main-thread → graceful degrade (no alarm, no
      crash, docstring note). Disarm sequence (SIG_DFL race, P1-2):
      `setitimer(0)` → set SIG_IGN → restore previous handler.
- [x] 1.3 Budget `except` arm sits OUTERMOST in `_run` (covers factory
      call, stage ladder, post-send region :284-289, AND teardown);
      `main` additionally carries a backstop
      `except _SessionBudgetExceeded` → same structured line + 69, so
      no window can surface as SMTP-INTERNAL-ERROR rc=70. Structured
      line: `SMTP-FAILED stage=<stage> host=<host>
      reason=session-budget elapsed=<s> budget=<s>` → EXIT_UNAVAILABLE.
- [x] 1.4 Post-250 rule: track `wire_outcome` (None until
      `send_message` returns; the refused dict after). Budget expiry
      with `wire_outcome` bound → proceed with normal post-send
      reporting (SMTP-ACCEPTED / SMTP-PARTIAL-REFUSAL) — a fired alarm
      must not unsay a received 250. Teardown reads `budget_tripped`
      to choose `smtp.close()` (non-blocking, verified) over `quit()`;
      normal-path `quit()` stays alarm-bounded (blanket except
      swallows).
- [x] 1.5 Factory per-op timeout → `min(SMTP_TIMEOUT_SEC, remaining)`
      (remaining = budget − elapsed at the connect call; ≈ budget at
      defaults, so effectively 30 s — stated honestly: SIGALRM is the
      real enforcement, this only caps per-op below budget for small
      env budgets, keeping the F6 pin :547 green at defaults).

## 2. Lane (scripts/node27_frontier_stall_alert.py)

- [x] 2.1 `except subprocess.TimeoutExpired`: decode `error.stderr`
      (may be None), append bounded tail to SendResult.error; rc stays
      124; nothing else changes.

## 3. Runbook (docs/runbooks/current-production-ops.md)

- [x] 3.1 At :2108-2111 (NOT the issue's stale :2033-2036 — that is
      §10.4 dry-run text): replace "nested" wording with per-op 30 s /
      session budget 45 s (env single-point) / lane 60 s SIGKILL
      fallback; rc=124-without-stage read-out incl. "message may
      already be delivered, next tick resends by design". Also add
      `reason=session-budget` to the exit-code table's `69` row
      (:2103-2105). The :2196-2198 tick-budget sentence stays correct;
      touch only if wording would otherwise contradict.

## 4. Tests

- [x] 4.1 Alignment guard (cross-module, with margin).
- [x] 4.2 Slow-per-op fake + tiny env budget → rc=69 before wall,
      `reason=session-budget` + stage on stderr (no real 45 s sleeps —
      scale via env override).
- [x] 4.3 Mid-op-blocked (dribbling-equivalent) fake → interrupted
      within budget without external kill, rc=69, correct stage.
- [x] 4.4 Stage attribution across phases (login vs send).
- [x] 4.5 Env override validation (bad → rc=64; absent → default).
- [x] 4.6 Success path unchanged + alarm disarmed + handler restored
      after main() returns.
- [x] 4.7 Teardown: expiry → close() called, quit() not called.
- [x] 4.8 Lane stderr fold: populated stderr folded; None-stderr
      unchanged.
- [x] 4.9 Existing shim tests (34 functions / 46 collected items) +
      lane tests (95 collected) green, ZERO modifications.
      Fixture-review ruling: neither F6 pin breaks — :527 calls the
      factory directly (immune), :547 asserts SMTP_TIMEOUT_SEC which
      min(30, remaining≈45) still yields at defaults; preconditions:
      default budget ≥ SMTP_TIMEOUT_SEC and no global env leak of
      NHMS_SMTP_SESSION_BUDGET_SEC (tests use the _env() dict helper).

## R1. Round-1 verified findings (fix pass @ 8490b8a4)

- [x] R1.1 CORR-1 (P2): disarm exception-safe end to end —
      `_SessionBudgetExceeded` from anywhere inside `disarm()` never
      propagates to the caller; handler restore unconditional (covers
      the prologue sub-window); after disarm the timer is inert and
      SIGALRM restored on every path. New test via widened-window seam
      (setitimer proxy): ACCEPTED printed + expiry in disarm → rc=0,
      single evidence line, handler restored.
- [x] R1.2 INT-1 (P2): `SESSION_BUDGET_CEILING_SEC = 60.0` shim-local;
      `_budget_seconds` rejects env values >= ceiling → rc=64;
      alignment test extended: ceiling <= lane SENDMAIL_TIMEOUT_SEC;
      validation tests (90 → rc=64 with SMTP-CONFIG-ERROR, sub-ceiling
      accepted); `.example` SMTP block commented entry; runbook
      :2122-2124 states the ceiling and the guard now truthfully
      covers the override.
- [x] R1.3 INT-2 (P2): runbook rc=124 read-out clause — the
      may-already-be-delivered final-dot window applies to ANY
      `stage=send` expiry/timeout line (session-budget AND
      error=timeout), not only stage-less rc=124.
- [x] R1.4 CORR-3 (Note): factory timeout clamped
      `max(0.001, min(SMTP_TIMEOUT_SEC, remaining))` so a
      negative/zero remaining can never surface as ValueError rc=70;
      pin: remaining forced negative → factory receives 0.001.
- [x] R1.5 TEV-1 (P2): SIG_IGN interposition pinned —
      `shim.signal.signal` recorder, assert SIG_IGN among installed
      handlers during disarm.
- [x] R1.6 TEV-2 (Note): min() cap pinned in its live regime —
      success path with env budget 0.30, assert factory timeout
      <= 0.30 (existing :553 default pin untouched).
- [x] R1.7 TEV-3 (Note): `elapsed=` substring asserted in an existing
      budget-expiry test.
- [x] R1.8 round-2 (P1, test-state leak): the raising_disarm test patched
      away the only code that stops the timer, leaking a live 45 s
      SIGALRM + the discarded budget's `_fire` handler into the pytest
      process (per-file CI green, master's full run poisoned). In-body
      try/finally cleanup in that test + module-level autouse guard
      fixture that repairs THEN reports any SIGALRM state a test leaves
      behind.
- CORR-2 verdict: CONFIRMED but DISCARD (lane consumes `accepted[-1]`;
  receipt provably undamaged). Optional zero-cost reorder
  (mark reported before printing) only if the region is touched
  anyway; skipping needs no reason.

## 5. Spec delta

- [x] 5.1 MODIFIED requirement in `specs/frontier-stall-alerting/spec.md`
      (byte-faithful + session-budget refinement + scenarios).

## 6. Evidence Floor

- [x] 6.1 `uv run pytest -q tests/test_node27_frontier_smtp_sendmail.py
      tests/test_node27_frontier_stall_alert.py` green (report counts).
- [x] 6.2 Red evidence: budget tests fail against unmodified shim
      (honest labels for any naturally-green pins).
- [x] 6.3 `uv run ruff check .` passes (per issue Verification field).
- [x] 6.4 `openspec validate smtp-shim-session-budget --strict
      --no-interactive` passes.
- [x] 6.5 Diff inspection: D2 retry semantics untouched; exit-code
      contract unchanged; SMTP-ACCEPTED evidence chain byte-identical.
