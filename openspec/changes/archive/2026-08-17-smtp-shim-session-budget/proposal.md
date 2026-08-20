# Proposal: smtp-shim-session-budget

## Why

Issue #1375 (PR #1374 verifier PLAUSIBLE-DEFER, tracked separately): the
SMTP shim's `SMTP_TIMEOUT_SEC = 30.0` is a PER-OPERATION socket timeout
(each blocking read/write restarts the clock), while the lane's
`SENDMAIL_TIMEOUT_SEC = 60` is a WALL-CLOCK SIGKILL. A session has ≥8
bounded reads (connect/TLS/greeting/ehlo/login≈2-3 round trips/MAIL/
RCPT×n/DATA/final-dot), so worst case ≈ 8×30 s = 240 s ≫ 60 s, and a
dribbling peer (a byte every <30 s) resets the per-op timer
indefinitely — the shim's session is effectively unbounded and only the
lane's SIGKILL ends it. Consequences: (1) stage attribution is
swallowed (SIGKILL mid-DATA → receipt shows only rc=124 "sendmail timed
out", operator cannot distinguish "cannot reach 163" from "message
half-pushed"); (2) potential duplicate delivery (RFC 5321 final-dot
responsibility transfer: server may have committed before 250; next
tick unconditionally resends per design D2 row 4 → duplicate alert +
spurious `send_failures`). The two constants were introduced in
different commits (143a0fa8 / 3f150c8f) with incompatible dimensions,
never reconciled.

## What Changes

- **Shim session budget (recommended route)**: explicit monotonic
  session deadline, default 45 s, env-overridable
  (`NHMS_SMTP_SESSION_BUDGET_SEC`) for single-point alignment with the
  lane wall. Enforcement is a SIGALRM/`setitimer` deadline raised as an
  in-band exception so the CURRENT `stage` variable provides
  attribution: on expiry the shim prints
  `SMTP-FAILED stage=<stage> ... reason=session-budget elapsed=<s>`
  and exits 69 — BEFORE the lane's 60 s wall, on every path including a
  dribbling peer mid-operation (which stage-boundary-only checks
  cannot bound; that is why boundary checks alone are rejected).
  Per-op timeout passed to the factory becomes
  `min(SMTP_TIMEOUT_SEC, budget)`. Teardown after budget expiry must
  not re-block past the wall (hard `close()`, not protocol `quit()`,
  when the budget tripped; alarm stays armed through teardown so a
  hanging `quit()` is also bounded).
- **Alignment guard test**: cross-module assertion
  `shim SESSION_BUDGET default < stall_alert.SENDMAIL_TIMEOUT_SEC`
  (with margin) so either side changing alone goes red.
- **Lane-side cheap increment (issue 兜底增量)**:
  `except subprocess.TimeoutExpired` branch reads `error.stderr`
  (capture_output=True already populates it) and folds any shim stderr
  lines already written into the SendResult error — partial stage
  attribution when the SIGKILL fallback does fire. Does not replace
  the budget (the main path is "shim printed nothing yet").
- **Runbook** `docs/runbooks/current-production-ops.md` (:2108-2111 —
  fixture-review P1-1 corrected coordinate; issue's :2033-2036 points
  into the unrelated §10.4 dry-run procedure) plus the exit-code table
  :2103-2105 (`reason=session-budget` belongs under the `69` row):
  replace the "nested timeouts" wording with the accurate
  three-layer story (30 s per-op / 45 s session budget / 60 s lane
  SIGKILL fallback) + how to read rc=124 without a stage line (shim
  itself died; message MAY have been delivered; next tick will resend).

## Capabilities

- `frontier-stall-alerting`: MODIFIED requirement "Alerting internal
  failures SHALL only increase alerting, never suppress it" — the
  "every blocking call SHALL be time-bounded" clause gains the session-
  budget refinement + scenarios (budget exit before wall with stage
  attribution; dribbling peer bounded without SIGKILL; rc=124
  semantics demoted to shim-self-hang). Byte-faithful otherwise.

## Impact

- `scripts/node27_frontier_smtp_sendmail.py` (budget mechanism),
  `scripts/node27_frontier_stall_alert.py` (TimeoutExpired stderr fold
  only), `docs/runbooks/current-production-ops.md`,
  `tests/test_node27_frontier_smtp_sendmail.py` (+ alignment guard;
  existing 34 test functions / 46 collected items must stay green
  unmodified),
  `tests/test_node27_frontier_stall_alert.py` (stderr-fold test).
- Out of scope (issue boundary): D2 retry semantics (failed send not
  recorded → next tick retries — the sanctioned over-report
  direction); delivery dedup/message-id idempotency; TLS verification,
  From/auth coupling, 8BITMIME (#1373 closed surface).
- Rejected alternative (issue-analyzed): shrinking SMTP_TIMEOUT_SEC —
  does not compose (8×12 s still > 60 s) and no effect on dribbling
  peers.
