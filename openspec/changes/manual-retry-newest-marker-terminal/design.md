# Design: Newest adopted marker terminates the attempt-derivation scan

## Context

`services/orchestrator/scheduler_state_manual_retry.py` holds two newest-first
scanners over the same adopted-marker event sequence:

- `_manual_retry_payload`: breaks unconditionally at the first (newest)
  adopted marker; sets `new_attempt` on the payload only when that marker's
  `retry_count` is non-empty AND `_marker_event_pins_attempt` holds.
- `_manual_retry_new_attempt`: first consults the payload's
  `new_attempt`/`retry_count`; when absent, re-scans events itself. Its scan
  arms today: non-marker → skip; marker with empty `retry_count` → **continue
  (walk back — the defect)**; marker with `retry_count` that does not pin →
  terminal fallback; marker with pinning `retry_count` → its value. Every
  fallback in the function routes through
  `_fallback_previous_attempt(state, previous_attempt) + 1` (the #1287
  restarted-stage-family floor).

Issue #1289 arrived `needs-triage`: the code change is small but requires a
semantic ruling first — does a newest adopted marker without `retry_count`
terminate the scan, or is the walk-back intentional (as the current in-code
comment claims)?

## Goals / Non-Goals

Goals:

- Record the ruling and encode it in code, tests, and the main spec.
- Give the previously test-invisible arm a discriminating oracle and an
  invariant anchor.

Non-goals (out of scope per issue #1289):

- Chain-side marker freshness/binding validation (#1201).
- Marker adoption/scope predicates (#1205, merged as PR #1286).
- The `_manual_retry_marker_shape` two-criteria oracle gap (separate note).
- Reserve/reclaim predicates; any consumer-chain modification.
- The db-free/DB read-path parity work (#1288 family).

## Decisions

### D1. Ruling: the newest adopted marker is the sole termination point

Adopted (the issue's recommended option): hitting the newest adopted marker
terminates the scan regardless of whether it carries a `retry_count`. No
`retry_count` means "no operator-specified attempt for this request" → return
the fallback; never walk back to an older marker.

Rationale:

- It is the natural generalization of the terminality principle the spec and
  the in-code comment already state for the pin-refusal arm ("falling through
  to an older own-model marker would replay an attempt number already
  consumed"). The walk-back arm lands on exactly the consequence that
  principle forbids.
- It unifies the two scanners' termination rules: `_manual_retry_payload`
  already treats the newest adopted marker as terminal, so today the
  attempt side can see older markers the payload side never reports —
  an operator reading `manual_retry` evidence cannot explain where the
  derived attempt came from.
- Rejected alternative — keep the walk-back, clamp the result to
  `max(derived, previous_attempt + 1)`: smaller diff and never returns a
  consumed number, but it hides the semantic question inside arithmetic
  (the walk-back still happens, its result silently raised), leaves the
  spec's terminality wording inconsistent with behavior, keeps the
  two-scanner split, and silently swallows the legitimacy question of an
  operator-specified low attempt instead of ruling on it.
- Rejected alternative — inverse alignment (make `_manual_retry_payload`
  walk back like the scan does, the direction the issue's 影响面 asks to
  check): it would export the consumed-replay defect into the payload's
  operator-facing evidence instead of removing it, and would contradict
  the terminality principle both the spec and the in-code comment already
  commit to.
- Deferred (recorded non-goal, not silent): the write-side option —
  require `retry_count` on every marker at the writer, fail-closed. That
  sits squarely in #1201's out-of-scope writer gap
  (`scheduler_state_failure.py` marker write side); this change rules on
  the reader's semantics only.

**Ruling domain**: the ruling governs the event-scan derivation only. A
state-level manual-retry attempt payload (a top-level `manual_retry` or
`manual_retry_marker` mapping carrying `new_attempt`/`retry_count` —
`_manual_retry_payload` reads the two keys through one `or` gate)
short-circuits ahead of
the event scan with no pin or adoption check; that route's semantics are
pre-existing, outside this ruling, and unchanged (the spec delta and the
invariant test are scoped accordingly — hardening that route belongs to
the #1201 writer-gap family, not here).

### D2. The new terminal arm returns the floored fallback, not bare `previous_attempt + 1`

The issue text (written before #1287 merged) says "返回 `previous_attempt + 1`".
Every existing fallback arm in `_manual_retry_new_attempt` routes through
`_fallback_previous_attempt(state, previous_attempt) + 1`; a bare
`previous_attempt + 1` on the new arm would re-introduce, on this one path,
exactly the unnameable-stage consumed-identity replay #1287 fixed. The new
arm uses the same floored fallback as its siblings. Recorded as a deviation
from the issue's literal wording in `tasks.md`; it strengthens, not weakens,
the issue's own invariant AC.

### D3. `_manual_retry_payload` is verified-aligned, not modified

The payload scanner already terminates at the newest adopted marker. The
implementer verifies the alignment claim (same event iteration, same adoption
predicate, unconditional break) and leaves the function untouched; the new
scenario asserts both scanners terminate at the same marker via observable
outputs (payload carries `requested` from the newest marker with no
`new_attempt` claim while the derivation returns the fallback).

### D4. Test strategy and oracles

All in `tests/test_file_orchestration_migration.py` and/or
`tests/test_production_scheduler.py`, next to the existing
`_manual_retry_new_attempt` anchors:

1. **Discriminating pair (issue AC, must be red pre-change)**: events =
   [older adopted own-model marker `retry_count=N` whose pin holds, newest
   adopted marker without `retry_count`], `previous_attempt=N` → derivation
   returns `N + 1` (fallback), not `N`. Parametrized over BOTH empty shapes
   of the newest marker's `retry_count` — field absent AND empty string —
   because the changed predicate is `value in (None, "")` and the empty
   string is the shape a writer persists for a blank operator field.
   (Narrowing that arm to `is None` is an EQUIVALENT mutant — `""` then
   reaches the pin arms and both return the same floored fallback, since
   `_coerce_int` answers its default on `int("")` — so this
   parametrization documents the predicate's domain; the mutation guard
   is the inverse-`continue` mutant of D4.5.) Red-proof
   protocol: implementer runs the new tests against the pre-change source
   (git stash or archive copy) and records the red output in the brief,
   exactly as done for #1287/#1288.
2. **Invariant test (issue AC)**: for a matrix of marker shapes carrying no
   pinning `retry_count` claim (no markers; newest marker without
   `retry_count`; newest marker whose `retry_count` does not pin), the
   derivation never returns ≤ `previous_attempt`. Domain scoped to states
   carrying no top-level `manual_retry`/`manual_retry_marker` attempt
   payload (see D1 ruling
   domain — the state-level short-circuit route has no pin check and is
   out of scope). The documented exemption: a newest adopted marker whose
   `retry_count` pins is an explicit operator claim and may legitimately
   be ≤ `previous_attempt` (existing behavior, anchored by the negative
   tests, not changed here).
3. **Negative anchors**: newest adopted marker WITH pinning `retry_count`
   still pins its value (honest reachability note: this shape returns via
   the payload's `new_attempt` short-circuit before the scan loop runs —
   the anchor guards the pinning outcome end-to-end, not the scan loop's
   own pin arm, which post-fix is reachable only through exotic state-level
   payload shapes); state with no adopted markers still derives the
   fallback; the pin-refusal arm (retry_count present, pin refused) still
   returns the fallback; and a marker-shaped event NEWER than the
   candidate's own pinning marker but NOT adopted (foreign attribution)
   neither terminates the scan nor decides — the candidate's own newest
   adopted marker still pins (honest reachability: that shape returns
   via the payload's `new_attempt` short-circuit before the scan loop
   runs, so this anchor guards the payload scanner's adoption predicate
   end-to-end; the scan loop's own adoption guard is anchored by the
   sibling scan-loop test, whose state-level `new_attempt: None` payload
   defeats the short-circuit so the scan governs — under the
   hoist-above-adoption-guard mutant its 5 becomes 1).
4. **Floor discrimination on the new arm (D2's oracle)**: newest adopted
   marker without `retry_count` on the unnameable-stage shape (cancelled
   own forecast row with consumed `_retry_2` suffix, no resolvable failed
   stage — the #1287 anchor geometry) derives the floored value 3, NOT the
   bare `previous_attempt + 1` = 1. This is the only case that
   discriminates `_fallback_previous_attempt(...) + 1` from bare
   `previous_attempt + 1` on the new terminal arm; without it the D2
   deviation would ship with zero test discrimination.
5. **Mutation guard (issue AC, restated post-fix)**: the issue's mutant
   ("replace the `continue` with a terminal return") IS this fix modulo the
   floor, and the discriminating pair is its kill. Post-fix the guard
   inverts: reintroducing the walk-back (replacing the new terminal return
   with `continue`) must turn the discriminating test red. The implementer
   demonstrates this by hand-applying the inverse mutant once, recording
   the red, and reverting — no mutation tooling added.

### D5. Spec delta wording

The requirement's terminality sentence stops using "retry-count-bearing" as
the deciding attribute: the scan is terminal at the newest adopted marker;
that marker alone decides, with or without a `retry_count`; the fallback
floor sentence generalizes from "the refused pin" to "neither a refused pin
nor an absent attempt claim". One new scenario captures the discriminating
shape and the two-scanner alignment. Per the standing symbol-anchor policy,
the change docs cite the touched file by symbol names only.

## Risks / Trade-offs

- **Behavior change surface**: any production state whose newest adopted
  marker lacks `retry_count` while an older marker carries one now derives
  the fallback instead of the older value. When the older value was consumed
  (== `previous_attempt`) the old result was a guaranteed silent no-op, so
  the change strictly improves that case. When the older value was NOT yet
  consumed, the old code pinned a stale claim from a superseded request —
  under the ruling that was never legitimate: the newer request explicitly
  carried no attempt claim. The negative anchors bound the delta to exactly
  this shape.
- **Latent, no live evidence**: the shape needs a specific marker sequence;
  severity is bounded (p2), but the arm currently has zero test
  discrimination, so the main risk being bought down is silent regression
  flips in future refactors.

## Migration

None. Pure derivation-semantics fix; no schema, storage, or API change.

## Risk pack selection (issue-risk-contract core packs)

- Concurrency / shared state / ordering: **selected** — persisted
  scheduler-state event ordering decides the derivation; the discriminating
  pair is an ordering oracle.
- Public API / CLI / script entry: not selected — module-internal helper,
  no signature change.
- Config / project setup: not selected — no config surface.
- File IO / path safety / overwrite: not selected — no file writes.
- Schema / columns / units / field names: not selected — no
  schema/field change (event `details.retry_count` is read, not reshaped).
- Auth / permissions / secrets: not selected — none involved.
- Resource limits / performance: not selected — one-arm control-flow
  change in an in-memory scan; no new allocation or IO.
- Legacy compatibility / examples: not selected as a pack — the behavior
  delta's compatibility bound is analyzed in Risks/Trade-offs (the only
  affected shape is the defective one).
- Error handling / rollback: not selected — no new error path; the arm
  returns a value, never raises.
- Release / packaging: not selected — no packaging, dependency, or
  entry-point change.
- Documentation / migration notes: not selected as a pack — spec delta is
  the documentation; Migration section records none needed.
