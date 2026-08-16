# Tasks: smtp-shim-session-budget

## 1. Shim (scripts/node27_frontier_smtp_sendmail.py)

- [ ] 1.1 `SESSION_BUDGET_SEC = 45.0` + env override
      `NHMS_SMTP_SESSION_BUDGET_SEC` (validated; bad value →
      _UsageError → rc=64); constant comment names
      `SENDMAIL_TIMEOUT_SEC` as the alignment partner.
- [ ] 1.2 SIGALRM/`setitimer` one-shot deadline; the HANDLER sets a
      `budget_tripped` flag THEN raises private `_SessionBudgetExceeded`
      (NOT an OSError subclass); armed after parse/config, before the
      factory call; non-main-thread → graceful degrade (no alarm, no
      crash, docstring note). Disarm sequence (SIG_DFL race, P1-2):
      `setitimer(0)` → set SIG_IGN → restore previous handler.
- [ ] 1.3 Budget `except` arm sits OUTERMOST in `_run` (covers factory
      call, stage ladder, post-send region :284-289, AND teardown);
      `main` additionally carries a backstop
      `except _SessionBudgetExceeded` → same structured line + 69, so
      no window can surface as SMTP-INTERNAL-ERROR rc=70. Structured
      line: `SMTP-FAILED stage=<stage> host=<host>
      reason=session-budget elapsed=<s> budget=<s>` → EXIT_UNAVAILABLE.
- [ ] 1.4 Post-250 rule: track `wire_outcome` (None until
      `send_message` returns; the refused dict after). Budget expiry
      with `wire_outcome` bound → proceed with normal post-send
      reporting (SMTP-ACCEPTED / SMTP-PARTIAL-REFUSAL) — a fired alarm
      must not unsay a received 250. Teardown reads `budget_tripped`
      to choose `smtp.close()` (non-blocking, verified) over `quit()`;
      normal-path `quit()` stays alarm-bounded (blanket except
      swallows).
- [ ] 1.5 Factory per-op timeout → `min(SMTP_TIMEOUT_SEC, remaining)`
      (remaining = budget − elapsed at the connect call; ≈ budget at
      defaults, so effectively 30 s — stated honestly: SIGALRM is the
      real enforcement, this only caps per-op below budget for small
      env budgets, keeping the F6 pin :547 green at defaults).

## 2. Lane (scripts/node27_frontier_stall_alert.py)

- [ ] 2.1 `except subprocess.TimeoutExpired`: decode `error.stderr`
      (may be None), append bounded tail to SendResult.error; rc stays
      124; nothing else changes.

## 3. Runbook (docs/runbooks/current-production-ops.md)

- [ ] 3.1 At :2108-2111 (NOT the issue's stale :2033-2036 — that is
      §10.4 dry-run text): replace "nested" wording with per-op 30 s /
      session budget 45 s (env single-point) / lane 60 s SIGKILL
      fallback; rc=124-without-stage read-out incl. "message may
      already be delivered, next tick resends by design". Also add
      `reason=session-budget` to the exit-code table's `69` row
      (:2103-2105). The :2196-2198 tick-budget sentence stays correct;
      touch only if wording would otherwise contradict.

## 4. Tests

- [ ] 4.1 Alignment guard (cross-module, with margin).
- [ ] 4.2 Slow-per-op fake + tiny env budget → rc=69 before wall,
      `reason=session-budget` + stage on stderr (no real 45 s sleeps —
      scale via env override).
- [ ] 4.3 Mid-op-blocked (dribbling-equivalent) fake → interrupted
      within budget without external kill, rc=69, correct stage.
- [ ] 4.4 Stage attribution across phases (login vs send).
- [ ] 4.5 Env override validation (bad → rc=64; absent → default).
- [ ] 4.6 Success path unchanged + alarm disarmed + handler restored
      after main() returns.
- [ ] 4.7 Teardown: expiry → close() called, quit() not called.
- [ ] 4.8 Lane stderr fold: populated stderr folded; None-stderr
      unchanged.
- [ ] 4.9 Existing shim tests (34 functions / 46 collected items) +
      lane tests (95 collected) green, ZERO modifications.
      Fixture-review ruling: neither F6 pin breaks — :527 calls the
      factory directly (immune), :547 asserts SMTP_TIMEOUT_SEC which
      min(30, remaining≈45) still yields at defaults; preconditions:
      default budget ≥ SMTP_TIMEOUT_SEC and no global env leak of
      NHMS_SMTP_SESSION_BUDGET_SEC (tests use the _env() dict helper).

## 5. Spec delta

- [x] 5.1 MODIFIED requirement in `specs/frontier-stall-alerting/spec.md`
      (byte-faithful + session-budget refinement + scenarios).

## 6. Evidence Floor

- [ ] 6.1 `uv run pytest -q tests/test_node27_frontier_smtp_sendmail.py
      tests/test_node27_frontier_stall_alert.py` green (report counts).
- [ ] 6.2 Red evidence: budget tests fail against unmodified shim
      (honest labels for any naturally-green pins).
- [ ] 6.3 `uv run ruff check .` passes (per issue Verification field).
- [ ] 6.4 `openspec validate smtp-shim-session-budget --strict
      --no-interactive` passes.
- [ ] 6.5 Diff inspection: D2 retry semantics untouched; exit-code
      contract unchanged; SMTP-ACCEPTED evidence chain byte-identical.
