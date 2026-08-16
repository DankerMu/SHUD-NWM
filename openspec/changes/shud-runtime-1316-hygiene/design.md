# Design: shud-runtime-1316-hygiene

## Change surface

Single file `workers/shud_runtime/runtime.py`. Current-head coordinates (the
issue cites `a6c14324` of the #1315 branch; line numbers have drifted —
re-locate by symbol, these are the head positions): failure-message raise site
:729-738 (`observed_text` built :731, message assembled :735-738);
`recovery_outcome_summary` :3477-3483; `install_recovered` :3546+ (re-entry
guard is the `target_info is None or hour in self.captured` clause near its
top); `TASK_OUTCOME_MESSAGE_MAX_LENGTH` :101, truncation applied :2326.

Risk triage: compact fixture. No I/O change, no schema change, no consumer
change. Highest risk is deleting the WRONG clause (the body-gate trap named
in the proposal) and ONE order-sensitive existing assertion (fixture review
F1): tests/test_shud_runtime.py:4335 slices the message with
`.split("(")[0]` to isolate the missing-hours list — the reorder moves
`; recovery outcomes:` (which contains `f012` in that test) in front of the
first `(`, flipping the assertion. Authorized fix: rewrite that ONE line to
the position-independent `.split(";")[0]` (valid under both orders: the
missing-hours list ends at the first `;` post-reorder, and pre-reorder the
segment before the first `;` contains only numerals). Every other existing
assertion is genuinely substring-only (enumerated in the F1 review), and no
non-test consumer parses this message.

## Key decisions

1. **Dead branches: split verdict, ruled at fixture time**:
   - DELETE the `install_recovered` re-entry guard; tracker-level invariant
     pins so re-introduction of a reachable path reds a test first:
     `missing_hours()` never yields an hour that is `None` in `targets` or
     already in `captured` (direct tracker-level test over a populated
     tracker, including after a successful `install_recovered` of a sibling
     hour — pins that capturing hour A does not mark hour B captured), plus
     `recovery_outcome_summary()` non-empty rendering pinned; the run-level
     truncation test exercises the real raise path.
   - RULED at fixture time (review F2 — no implementer coin flip): deleting
     the empty guard WOULD leave the degenerate `"; recovery outcomes: "`
     rendering, so the split verdict is fixed: DELETE the re-entry guard in
     `install_recovered` (:3554-3556, + tracker-level invariant pin); KEEP
     the empty guard in `recovery_outcome_summary` (:3480-3481) as a
     total-function contract, WITH an in-code comment (per the issue's
     "保留+注释" arm) naming the single call site (the raise at :737) and why
     `recovery_outcomes` is provably non-empty there — so the next reviewer
     does not re-run the mutation probe.
2. **Message ordering** (review F3 refined): assemble as missing-hours list →
   `recovery_outcome_summary()` → `manifest_note` → observed-minutes
   parenthetical LAST. Rationale: truncation at the receipt layer cuts the
   tail; the two bounded, actionable fields (recovery outcomes: one entry per
   missing hour; manifest_note: tells the operator the fallback manifest
   itself failed to land) must both precede the unbounded observed trail
   (grows with run duration). The
   `(observed cfg.ic.update header minutes: ...)` phrase itself stays intact
   (tests grep it); tests :4465/:4742 on manifest_note are plain substring
   checks and stay green.
3. **Minute rendering** (review F4/F5 refined): extract a
   `_format_header_minute(minute: float) -> str` helper rendering lossless
   plain decimal — `str(int(minute))` when `minute == int(minute)`, else
   `repr(minute)`; non-finite input (nan/inf — reachable via the bare
   `float(token)` parse) falls back to `repr(minute)` BEFORE the integrality
   check, because `int(nan)` raises and this lane's contract (runtime.py
   :714-717) is that diagnostics never change the error code (review N1);
   `:.0f` is FORBIDDEN (header minutes are NOT integral by
   construction: `_read_cfg_ic_header_minute` returns whatever float parses,
   and `:.0f` would silently round). Use the helper at BOTH lossy sites: the
   observed trail (:731) and the recovery-outcome
   `gate_rejected(header={...})` rendering (:3588) — otherwise the trail this
   change promotes to the front can itself still read
   `gate_rejected(header=2.96078e+07)`. The helper is the unit-test seam for
   acceptance 3 (no new full-run epoch stub required). `720`/`1440` render
   unchanged (test asserts `: 1440`).

## Must preserve

- `run_shud`'s `missing_hours()` short-circuit before recovery — KEEP
  (adjudicated in the issue; prevents spurious cfg read).
- The body gate `state_ic_structure_complete` clause and its pin test
  (`test_run_shud_recovery_rejects_structurally_truncated_state`) — NOT the
  clause being deleted; do not touch.
- Existing message substrings: `observed cfg.ic.update header minutes:`,
  `recovery outcomes:`, error code `STATE_CHECKPOINTS_MISSING`.
- Receipt fields: `error_code` untouched; truncation threshold untouched.
- Manifest content and `state_checkpoints.json` write path untouched.

## Seams under test

- Tracker level: direct `_StateCheckpointTracker` construction (new — existing
  tests don't call `install_recovered` directly).
- Run level: the existing `run_shud`/`execute()` failure-path fixtures in
  tests/test_shud_runtime.py (STATE_CHECKPOINTS_MISSING patterns at
  :3576/:4725) for the truncation test; rendering is unit-tested on the
  `_format_header_minute` seam directly.

## Test plan (requirement-driven, maps to acceptance)

1. Dead-branch invariants pinned at tracker level (acceptance 1).
2. Truncation survivability: construct >512-char observed trail; exercise
   the REAL production truncation via `_write_task_outcome_receipt` (directly
   callable — existing pattern at tests/test_shud_runtime.py:3168); assert
   the receipt's `error_message` still contains `recovery outcomes:`
   (acceptance 2). No slice-simulation fallback (review F6).
3. `_format_header_minute` unit tests: `29607840.0` → `"29607840"` (no
   scientific notation), `1440.0` → `"1440"`, a non-integral value →
   lossless `repr` (acceptance 3); relative-minute message expectations
   unchanged.
4. `run_shud` short-circuit regression: assert the guard still present via
   existing behavior tests staying green (acceptance 4 is a no-change check).
5. No assertion's intent changed; the single enumerated delimiter rewrite
   (:4335) is the only existing-test edit.

## Risks to watch

- The two guards look similar to neighboring live gates — delete by exact
  clause identity, not by pattern.
- `observed_header_minutes` values may be float-typed from cfg parsing even
  when integral; rendering choice must handle both without changing existing
  test expectations.
