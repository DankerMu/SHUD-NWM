# Tasks: shud-runtime-1316-hygiene

## 1. Implementation (workers/shud_runtime/runtime.py)

- [x] 1.1 Dead-branch disposition (RULED, design decision 1): delete the
      `install_recovered` re-entry guard (:3554-3556); KEEP the
      `recovery_outcome_summary` empty guard with an in-code comment naming
      the single call site (raise at :737) and the non-emptiness proof.
      Exact-clause identity; do NOT touch the body gate
      (`state_ic_structure_complete`).
- [x] 1.2 Failure-message reorder (design decision 2): missing-hours list →
      `recovery_outcome_summary()` → `manifest_note` → observed-minutes
      parenthetical last; existing substrings preserved.
- [x] 1.3 Extract `_format_header_minute` (lossless plain decimal, design
      decision 3; non-finite → `repr` fallback before the integrality check)
      and use it at BOTH `:g` sites (:731 observed trail, :3588
      gate_rejected header); `720`/`1440` render unchanged.

## 2. Tests (tests/test_shud_runtime.py)

- [x] 2.1 Tracker-level invariant pins (missing_hours ↔ captured/targets;
      sibling-hour non-contamination after install_recovered; summary
      rendering non-empty case).
- [x] 2.2 Truncation survivability end-to-end (review N2 — the message MUST
      originate from the production assembly, not a hand-built string, or
      the test is vacuous w.r.t. ordering): drive the failure via
      `execute()` (which writes the receipt itself on a failed attempt,
      runtime.py:471-474; assertion helper
      `_assert_attempt_failure_accounting` at tests/test_shud_runtime.py
      :6140-6161) or at minimum catch the real `run_shud` SHUDRuntimeError
      and feed it to `_write_task_outcome_receipt` (:3168 pattern); grow the
      observed trail deterministically past 512 chars by monkeypatching
      `_read_cfg_ic_header_minute` to yield a fresh minute per call
      (precedent :4367-4377). Assert receipt `error_message` still contains
      `recovery outcomes:`.
- [x] 2.3 `_format_header_minute` unit tests: `29607840.0` → `"29607840"`,
      `1440.0` → `"1440"`, non-integral → lossless `repr`; relative-minute
      message expectations untouched.
- [x] 2.4 No assertion's INTENT changed; exactly ONE enumerated delimiter
      rewrite: tests/test_shud_runtime.py:4335 `.split("(")[0]` →
      `.split(";")[0]` (order-independent, review F1). The
      structurally-truncated-state test and the `run_shud` short-circuit
      stay untouched and green.

## 3. Spec delta

- [x] 3.1 MODIFIED `cross-cycle-warm-start-chaining` "Forecast checkpoint
      mechanics" requirement — one appended scenario (truncation
      survivability + lossless rendering), byte-faithful otherwise.

## Evidence Floor

- `uv run pytest -q tests/test_shud_runtime.py` green.
- `git ls-files '*.py' | xargs uv run ruff check` green.
- `openspec validate shud-runtime-1316-hygiene --strict --no-interactive`
  valid.
- Red proof for (2)/(3): message-shape tests red against pre-change code
  (truncated receipt lacking `recovery outcomes:` / scientific notation
  present), captured before implementing.
- Dead-branch deletion evidenced by mutation-probe reasoning already in the
  issue + new invariant pins green.
