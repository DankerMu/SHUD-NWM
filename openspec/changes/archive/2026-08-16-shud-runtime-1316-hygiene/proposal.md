# Proposal: shud-runtime-1316-hygiene

## Why

Issue #1320: three verified-non-blocking hygiene items left over from PR #1316
(#1315 checkpoint capture race), all in `workers/shud_runtime/runtime.py`,
batched into one cleanup:

1. Two dead branches, both mutation-probed during #1316 round-4 review
   (forced raise → full suite stays green): the re-entry guard in
   `_StateCheckpointTracker.install_recovered` (both sub-conditions provably
   false at the only call site, whose `hour` comes from `missing_hours()`),
   and the empty guard in `recovery_outcome_summary` (the raise site is only
   reachable after recovery ran, and every recovery exit records an outcome).
2. `STATE_CHECKPOINTS_MISSING` failure-message suffix ordering: the unbounded
   observed-header-minutes trail precedes `; recovery outcomes: ...`, so the
   task-outcome receipt's 512-char truncation
   (`TASK_OUTCOME_MESSAGE_MAX_LENGTH`, applied at the receipt write) drops the
   MOST actionable field first (measured: 24h@10-min run → 901-char message →
   recovery outcomes entirely gone from the receipt).
3. `f"{minute:g}"` renders epoch-form header minutes in scientific notation
   (`29607840.0` → `2.96078e+07`), breaking greppability against the
   manifest's full-precision values.

## What Changes

- Split verdict on the two dead branches (ruled at fixture time, review F2):
  DELETE the `install_recovered` re-entry guard, with tracker-level unit
  tests pinning the invariants that make it dead (the `missing_hours()` ↔
  `captured`/`targets` relation, sibling-hour non-contamination), so a
  future change that re-introduces a reachable path reds a test first; KEEP
  the `recovery_outcome_summary` empty guard as a total-function contract,
  with an in-code comment (the issue's "保留+注释" arm) naming the single
  call site and the non-emptiness proof — deleting it would leave the
  degenerate `"; recovery outcomes: "` rendering for any future caller.
- Reorder the failure-message suffixes: missing-hours →
  `recovery_outcome_summary()` → `manifest_note` → observed-header-minutes
  trail LAST, so truncation sacrifices the only unbounded (and least
  actionable) field first. Add a test through the real receipt write:
  >512-char observed trail, receipt `error_message` still contains
  `recovery outcomes:`. One existing assertion needs a delimiter rewrite
  (tests/test_shud_runtime.py:4335 `.split("(")[0]` → `.split(";")[0]`,
  order-independent) — enumerated, intent unchanged; all other existing
  assertions are substring-only.
- Extract `_format_header_minute` (lossless plain decimal; `:.0f` forbidden —
  header minutes are not integral by construction) and apply at BOTH `:g`
  sites (observed trail and the recovery-outcome `gate_rejected(header=...)`
  rendering); tests pin no scientific notation for epoch-form input
  (`29607840.0` class).

## Capabilities

- `cross-cycle-warm-start-chaining`: MODIFIED requirement "Forecast
  checkpoint mechanics are functional, not assumed" — one appended scenario
  (truncation survivability + lossless minute rendering). Byte-faithful
  otherwise.

## Impact

- Single file `workers/shud_runtime/runtime.py` + `tests/test_shud_runtime.py`
  (new tests; one enumerated delimiter rewrite at :4335, intent
  unchanged; no other existing assertion modified).
- Out of scope (issue boundary): #1315 capture-race semantics, #1317
  alignment guards, the `run_shud` `missing_hours()` short-circuit before
  recovery (VERDICT: KEEP — it prevents a spurious cfg read on the
  no-missing path; already adjudicated in the issue, do not re-review),
  receipt schema/version, `TASK_OUTCOME_MESSAGE_MAX_LENGTH` threshold.
- Known trap recorded: `tests/test_shud_runtime.py`'s
  `test_run_shud_recovery_rejects_structurally_truncated_state` pins the BODY
  gate (`state_ic_structure_complete`), NOT the re-entry guard being deleted —
  different clause, must not be touched.
