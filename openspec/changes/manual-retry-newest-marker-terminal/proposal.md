# Proposal: Manual-retry attempt scan terminates at the newest adopted marker

## Why

`_manual_retry_new_attempt` (`services/orchestrator/scheduler_state_manual_retry.py`)
derives the manual-retry attempt number by scanning candidate events newest-first.
When the newest adopted marker carries no `retry_count`, the scan's
`value in (None, "")` arm executes `continue` and walks back to an OLDER adopted
marker — and if that older own-model marker carries an already-consumed
`retry_count == previous_attempt`, the function returns the consumed number
verbatim (issue #1289, reproduced read-only at the issue's HEAD and re-verified
at current master). The consumed identity then flows through the manifest into
the minted `<stage>_retry_<N>` job id, where the reservation boundary reads it
as a duplicate and silently skips submission — the same operator-visible
failure mode #1201/#1164 documented, but originating in the scheduler-state
derivation itself.

The same file's sibling scanner `_manual_retry_payload` breaks unconditionally
at the first (newest) adopted marker, so the two scanners apply two different
termination rules to the same event list. The function's own comment already
states the governing principle — "A non-pinning hit is TERMINAL: falling
through to an older own-model marker would replay an attempt number already
consumed" — but only the pin-refusal arm honors it. 1442 existing tests across
the four affected suites have zero discrimination on this line (issue #1289
mutation evidence).

## What Changes

- **Semantic ruling (issue was needs-triage; ruled here, recommended option
  adopted)**: within the event-scan derivation — a state-level top-level
  `manual_retry`/`manual_retry_marker` attempt payload short-circuits ahead
  of the scan and stays outside this ruling (design.md D1 "Ruling domain")
  — the newest adopted
  marker is the sole termination point. A newest adopted marker whose
  `retry_count` is absent or empty makes no operator attempt claim and
  yields the fallback (`_fallback_previous_attempt(state, previous_attempt)
  + 1`); older adopted markers are never consulted, and an UNADOPTED
  marker-shaped event neither terminates nor decides. This generalizes the
  already-specified terminality principle from the pin-refusal arm to all
  non-pinning outcomes and aligns the scan with `_manual_retry_payload`'s
  unconditional break.
- Code: replace the walk-back `continue` arm in `_manual_retry_new_attempt`
  with a terminal fallback return (one arm; no signature or consumer change).
  `_manual_retry_payload` is verified already-aligned and left untouched.
- Tests (design.md D4): a discriminating pair (red at pre-change HEAD,
  parametrized over absent and empty-string `retry_count`), an invariant
  test (never ≤ `previous_attempt` absent an operator pin claim, domain
  scoped to states without a top-level `manual_retry`/`manual_retry_marker`
  attempt payload),
  negative anchors (pinning newest marker, no markers, pin-refusal arm,
  newer unadopted marker-shaped event), and a floor-discrimination case on
  the new arm (D2's oracle).
- Spec: the job-retry-mechanism requirement's terminality sentence stops
  relying on "retry-count-bearing" to exclude this shape — the newest
  adopted marker decides within the scan's domain — plus a new scenario for
  the discriminating shape.

The rejected alternatives (walk-back kept with an arithmetic clamp; inverse
alignment of the payload scanner) and the deferred write-side fail-closed
option are recorded in `design.md` D1.

## Impact

- Affected specs: `job-retry-mechanism` (one requirement modified, one
  scenario added).
- Affected code: `services/orchestrator/scheduler_state_manual_retry.py`
  (`_manual_retry_new_attempt` scan loop, one arm + comment); tests in
  `tests/test_file_orchestration_migration.py` /
  `tests/test_production_scheduler.py`.
- Behavior delta is confined to the shape "newest adopted marker without
  `retry_count` above an older `retry_count`-bearing marker": previously the
  older marker's value leaked through; now the fallback decides. All other
  marker shapes are anchored unchanged by negative tests.
- Consumer chain (`scheduler_state_failure.py` → manifest → chain runtime) is
  read-only context; no consumer is modified.
