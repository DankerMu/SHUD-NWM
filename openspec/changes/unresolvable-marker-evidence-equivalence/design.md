# Design: Evidence-equivalent unresolvable-marker pin gate

## Context

`_marker_event_pins_attempt` routes an adopted marker to one of two rules:
entity resolves to a job row → `_cycle_scope_marker_pins_attempt` (the
resolved-row twin); no row → `_unresolvable_marker_entity_pins_attempt`. The
unresolvable arm exists because both production read paths routinely drop the
marker's target row (identity-filter cohort deletion; row-window truncation)
while the marker survives. Its docstring promises evidence equivalence with
the twin; issue #1292's three CONFIRMED defects (PR #1286 round-5, DEFERRED
as unreachable) show the promise fails in both directions because the arm
reads entity-id TEXT as its only evidence.

## Goals / Non-Goals

Goals: close all three defects in one rewrite; flip the evidence source to
the marker's own record; make the docstring's equivalence claim an executable
assertion; land before #1186 wires the first real caller.

Non-goals (out of scope per issue #1292): the twin's failure-domain gaps
(#1287 landed, #1294 tracks `cancelled` cohort targets); candidate-state
membership (#1288 landed); attempt-scan walk-back (#1289 landed); the db-free
manual-retry execution entry itself (#1186); `job_limit` truncation attempt
reset (#1179); any DB-path predicate change.

## Decisions

### D1. Evidence source: marker record primary, loop-stripped id text as backstop

Adopted (the issue's recommended structural option, credited to the round-5
invariant-state reviewer): the pin judgment uses the marker's OWN recorded
stage, not id-text forensics.

- Writer half: `record_manual_repair` already returns `stage:
  failed_job.get("stage")` on its API namespace but omits it from the
  persisted event `details`. Port it as `details["failed_stage"] =
  failed_job.get("stage")`. Additive, schemaless journal JSON; no migration.
- **Key name is `failed_stage`, NOT `stage` — a load-bearing choice** (the
  fixture review's consumer analysis): the candidate-state builder's
  record-stage reader (`chain_repository_state._normalized_record_stage`)
  consumes `details.stage`/`details.job_type` from EVENT records and feeds
  `_record_allowed_for_compute_state_terminal`; writing `details["stage"]`
  would make the marker event itself disappear from the candidate state for
  legacy-downstream-stage targets under the production terminal-stage
  setting — the operator's retry would stop reporting
  `manual_retry_requested` at all. `failed_stage` is read by no record-stage
  consumer, and matches the state-level key it is compared against.
- Sanitizer whitelist: the identity-filter event sanitizer
  (`scheduler_state_identity_filter`) whitelists detail keys on retry
  events; `failed_stage` is added to that carve-out list (one line) so the
  evidence survives the exact mechanism — identity-filter rewrite — that
  creates the row-absent shape. Third production file touched, additive.
- Reader half: the router passes the EVENT to the arm; the arm reads
  `details.failed_stage` as the primary stage evidence.
- Domain note (journal projection): at cycle-scope completion stages the
  journal read path compacts model-less cycle-scope events by dropping
  `details` entirely, so those markers fail the marker-shape predicate and
  are never adopted — the pin gate's live domain on the journal path is the
  submission stages. Discriminating fixtures therefore operate on
  synthesized state mappings (as the round-5 verifier did), with
  submission-stage cohort ids where journal-path realism is claimed.
- Backstop for stage-less markers (legacy/synthesized states; every marker
  written before this change — today that set is empty in production, zero
  marker events on the node-27 archive): fall back to the id-text token, but
  only after loop-stripping ALL `_retry_<n>` suffixes with
  `retry_identity.split_retry_job_identity` (the module's own docstring:
  suffixes stack, the LAST is authoritative; single `rsplit` strips one
  layer — the node-27 three-layer receipt
  `..._state_save_qc_retry_1_retry_2_retry_3` motivates the stacked-suffix
  grammar; the discriminating fixtures use submission-stage ids per the
  domain note below and D4.1).
- Rejected: pure id-text repair (the issue's fallback option) — any future
  suffix grammar or a stage name containing underscores reopens the class;
  the spec would keep endorsing text matching.
- Rejected: refusing to pin stage-less markers outright — turns the backstop
  gap into a silent under-pin for exactly the synthesized states the
  fail-open exists to serve.

### D2. Staleness conjunction (defect 2), row-absent evidence

Mirror the twin's refusal ORDER (staleness before stage). With no row, the
staleness evidence is the state-level `repaired_stage_evidence` mapping:
refuse the pin when the mapping's `original_failed_job_id` names the
marker's target — **exact string comparison against the marker's entity id**
(the twin's staleness test is about the entity row, so the row-absent
equivalent keys on the entity id; no `previous_job_id` priority, no
suffix-aware stripping — suffix-stripping both sides would make a
still-failed `..._retry_3` target match its repaired `..._retry_2` ancestor
and refuse a pin the twin would grant, a new under-pin in defect 1's own
direction). This deliberately differs from the
`_manual_retry_marker_repairs_historical_failure` row-absent read (which
keys on `previous_job_id` plus a repairing-retry-id conjunct) because that
read answers a different question — "does this marker repair the historical
failure" — while this one asks "is this marker's own target already
repaired".

**AC-2 scope (fixture-review adjudicated, twice-narrowed)**: verdict
identity between row-present and row-absent is delivered IN FULL only for
repaired targets the state's `repaired_stage_evidence` names as its
original failed job — the one staleness sub-shape with row-absent evidence.
The twin's repaired-target refusal fires on either of two ROW flags
(`repair_status == "repaired"` or `active_blocker is False`,
`_pipeline_job_is_repaired_stage_evidence`); a target carrying only those
flags that the state mapping does not name has no row-absent evidence and
joins Residue 1 alongside placeholder/non-failed staleness (which lives on
the row's own status). With the row absent those targets pin where the twin
refuses, and on stage mismatch arm-2 may answer True where the twin answers
False. This residue is an accepted, issue-tracked gap (see Residues below),
not a claimed equivalence; the tests anchor arm-2 blocking behavior (a live
failure elsewhere blocks the pin) without asserting cross-arm verdict
identity for those shapes. The identity fixture must therefore carry BOTH
halves: the repaired flag on the target row (row-present half) AND the
state's `repaired_stage_evidence` naming that job (row-absent half).

### D3. Arm-2 fall-through (defect 3)

Stage mismatch falls through to `not
_state_has_candidate_scope_failed_job(state)` exactly as the twin does —
same predicate object, no restatement, so #1287's widened live-failure
domain applies identically on both arms.

### D4. Test plan (all red-proof items follow the #1287-#1289 protocol)

1. Defect-1 discriminating pairs (red pre-change): single-suffix and
   three-layer ids, each through BOTH absence mechanisms (identity-filter
   cohort deletion; row-window truncation with a newer same-stage row) —
   on synthesized state mappings, with submission-stage cohort ids
   (`download`/`forcing`/`convert`) as the journal-realistic geometry; the
   node-27 `..._state_save_qc_retry_1_retry_2_retry_3` receipt remains the
   motivation for the stacked-suffix GRAMMAR, not a journal-path fixture
   (completion-stage compaction strips its details before adoption — D1
   domain note).
2. Defect-2 equivalence assertions (red pre-change for the repaired shape):
   row-present vs row-absent verdict identity on MAPPING-NAMED repaired
   targets only — the fixture carries the repaired flag on the target row
   AND the state's `repaired_stage_evidence` naming it (both halves, D2);
   arm-2 blocking anchors for placeholder/non-failed shapes without
   cross-arm identity claims (D2 AC-2 scope).
3. Defect-3 cross-arm parity (red pre-change): model-less cohort truncation
   + cross-stage failure — unresolvable verdict == resolved verdict.
4. Writer tests: persisted event details carry `failed_stage`; the marker
   event still enters the candidate state under the terminal-stage setting
   (the consumer non-drop anchor — guards the D1 key-name choice); the
   identity-filter sanitizer preserves `details.failed_stage` on retry
   events.
5. Non-regression: non-cycle-grammar fail-open; foreign cycle never pins;
   stage-less backstop (with and without suffixes); the #1205 committed
   anchor subset (M6 grammar anchor, T7/T8, same-cycle cohort anchor,
   truncation anchor, T9/T10, V-E 4-cell); SQL RetryService id shape
   (`{run_id}_retry_active`) stays fail-open.
6. Mutation guards: revert stage evidence to bare `endswith` (killed by
   three-layer pair); drop the staleness conjunction (killed by defect-2
   pair); restore `return False` over arm-2 fall-through (killed by
   defect-3 case); single-strip instead of loop-strip (killed by the
   STAGE-LESS three-layer backstop case — with `failed_stage` present the
   primary evidence decides and the mutant survives, so the kill is bound
   to the backstop fixture explicitly).

### D5. Spec delta

The main-spec clause "(the id ends with the state's failed stage, …)" inside
the cycle-scope pin scenario's filtering-equivalence AND clause is rewritten:
the surviving marker pins exactly when the id's cycle is the candidate's own
AND the marker's recorded stage (its `failed_stage` detail, with the
loop-stripped id token as the stage evidence for markers written before the
field existed) is the repair target, the exact-id repaired-evidence refusal
applies, and a cross-stage marker falls to the only-failure-left arm instead
of refusing outright — verdict equality with the resolved-row rule claimed
only for failed-status targets carrying no repaired-stage-evidence flags.
New scenario clauses pin the three defect
shapes. Symbol anchors only for PR-touched files.

## Residues (accepted, tracked — not claimed as delivered)

1. **Row-borne staleness evidence, row absent** (D2 AC-2 scope): a
   same-stage unsubmitted-auto-retry-placeholder or non-failed target — and
   a repaired-flagged target (`repair_status == "repaired"` or
   `active_blocker is False`) that the state's `repaired_stage_evidence`
   does not name — pins with the row absent where the twin refuses with it
   present; that evidence lives on the row and does not survive its
   absence. Tracked as a follow-up issue (see tasks.md closeout).
2. **F5′ (archived `manual-retry-marker-attribution` design.md): the
   model_id-bearing `job_cycle_*` row, row-absent ∧ cross-stage.** With the
   row present such a row is NOT cycle-scope (the cycle-scope predicate
   requires empty model_id) and the router answers True; with the row
   absent, this change's arm-2 fall-through still answers False under the
   F5′ premise (a cross-stage live failure exists). The under-pin residue
   therefore SURVIVES this change — restated here because the archived
   disclosure is frozen; tracked in the same follow-up issue.

## Risks / Trade-offs

- The arm's verdict changes on cycle-grammar unresolvable markers only; all
  three directions (two under-pin regressions fixed, one over-pin closed)
  move TOWARD the twin's semantics, and the twin itself is untouched.
- The new `details.failed_stage` field is additive AND deliberately named
  to be invisible to the one consumer that does read event detail keys by
  name: the candidate-state record-stage reader consumes
  `details.stage`/`details.job_type` and would have dropped the marker
  event under terminal-stage gating had the field been named `stage` (D1).
  The sanitizer whitelist addition is the mirror obligation: without it the
  identity-filter rewrite would strip the evidence on exactly the row-absent
  path it serves.
- Zero production markers exist today (node-27 receipt), so the backstop
  path has no live population — it exists for synthesized/legacy states and
  is exercised purely by tests until #1186 ships.

## Migration

None. Additive event field; no replay or backfill (no existing markers).

## Risk pack selection (issue-risk-contract core packs)

- Concurrency / shared state / ordering: selected — persisted event evidence
  and row-absence timing decide the verdict.
- Schema / columns / units / field names: selected — additive journal event
  detail field (`failed_stage`), writer-side test required.
- Public API / CLI / script entry: not selected — private helpers; the
  record_manual_repair signature is unchanged.
- Config / project setup: not selected — no config surface.
- File IO / path safety / overwrite: not selected — journal append path
  unchanged.
- Auth / permissions / secrets: not selected — policy-decision plumbing
  untouched.
- Resource limits / performance: not selected — O(suffix-depth) loop strip
  on one id per marker decision.
- Legacy compatibility / examples: not selected as a pack — the stage-less
  backstop IS the compatibility surface, designed and tested in D1.
- Error handling / rollback: not selected — the arm returns booleans, never
  raises; fail-open arm unchanged.
- Release / packaging: not selected — no packaging change.
- Documentation / migration notes: not selected as a pack — spec delta is
  the documentation; Migration records none needed.
