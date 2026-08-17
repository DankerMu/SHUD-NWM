# Design: retry-stage-failure-classification

Fixture level: compact (M; three coupled surfaces — code set, spec
prose, pin tests — but the semantic rulings are already made and the
behavior invariant is structural).

## Risk triage

- Primary risk: classification semantics drift — accidentally
  changing RETRY BEHAVIOR instead of audit labels. Mitigated
  structurally: `NON_TRANSIENT_ERROR_CODES`'s only production
  consumer is the reason ternary (retry.py:167 at final head, grep-verified);
  `is_retryable_failure` reads the transient list only. Evidence
  floor requires re-proving the single-consumer fact on the final
  head.
- Secondary risk: breaking #1419's freshly merged pins
  (`SLURM_JOB_FAILED` dual-absence at
  tests/test_real_slurm_gateway.py:1039-1040; classifier branch;
  TRANSIENT∩NON_TRANSIENT=∅ and dual-transient-face equality) —
  none of these touch our members, but the suite must stay green
  unmodified there.
- Tertiary risk: the spec-parsing window
  (`_spec_non_transient_error_codes`, tests/test_retry.py:74-95)
  has a strict format — bullets `- \`CODE\` — description` under the
  exact scenario header, terminated by THEN/heading. New bullets
  must match that shape or the parser silently misses them; the
  reconciliation test at :115 (spec set ∪ code-only frozenset ==
  code set) reds on any mismatch, which is the designed safety net.

## Decisions

1. **Membership (user ruling + fact correction)**: 13 new codes =
   SHUD trio + `f"{stage.upper()}_FAILED"` over
   `DOWNSTREAM_RESTART_STAGES` (7, the TRUE closed minting domain —
   both minting sites canonicalize through
   `_canonical_downstream_stage` which rejects anything outside) +
   the 3 enumerated task codes. NOT a suffix predicate: `_FAILED`
   endswith would swallow `SLURM_JOB_FAILED` (must stay dual-absent
   per #1419) and the TRANSIENT `SBATCH_SUBMISSION_FAILED` /
   `STORAGE_WRITE_FAILED`.
2. **In-code derivation for the stage family**:
   `retry.py` imports `DOWNSTREAM_RESTART_STAGES` from
   `scheduler_state_types` (cycle-free: that module imports stdlib
   only) and builds the stage members mechanically, so a future
   canonical stage auto-classifies. The SHUD trio and task codes
   stay literal members with one-line comments. Set construction
   shape: explicit literals unioned with the derived comprehension,
   in one place, with a comment naming this change and the #1419
   dual-absence constraint.
3. **Spec list placement**: all 13 codes go into the "Non-transient
   error codes block auto-retry" scenario bullet list in the
   existing `- \`CODE\` — rationale` format (the parser's window),
   auto-extending the end-to-end parametrized pin
   (`test_permanently_failed_event_carries_auto_retry_skipped_for_
   non_transient_codes` — each new code gets the full
   permanently_failed + reason + no-warning behavioral pin for
   free). `_CODE_ONLY_NON_TRANSIENT_ERROR_CODES` is NOT extended
   (that frozenset exists for codes the spec deliberately does not
   name; these are production-mainline codes the spec SHOULD name).
4. **New spec scenario pins the derivation**: "Stage-failure codes
   track the canonical downstream stage domain" — WHEN the canonical
   `DOWNSTREAM_RESTART_STAGES` domain contains a stage THEN
   `{STAGE}_FAILED` is in the non-transient classification set. The
   scenario sits OUTSIDE the parser window (parser stops at the
   scenario's THEN bullets / next heading — verified against the
   parser's termination rule), so it cannot double-feed the
   parametrized pin. Test-side: one new derivation pin test asserts
   set-inclusion of the derived family plus
   `retry.DOWNSTREAM_RESTART_STAGES is
   scheduler_state_types.DOWNSTREAM_RESTART_STAGES` — fixture-review
   Note-1: a runtime assertion cannot distinguish a derived
   comprehension from a hand copy, so the pin claims inclusion + the
   import edge, no more; the substance (a future canonical stage
   reds the pin) is fully carried by the inclusion assertion.
5. **Anchor swaps, never deletions**:
   - `_UNLISTED_PRODUCTION_ERROR_CODES` (tests/test_retry.py:56)
     becomes `("SLURM_JOB_FAILED",)` — its pinning test now guards
     ONLY the #1419 ruling; the comment gains one line recording
     that `SHUD_FAILED` moved to the classified list here (#1462).
   - The unknown-branch parametrized pin over that tuple keeps
     running for `SLURM_JOB_FAILED` (unknown reason + warning
     stays).
   - tests/test_real_slurm_gateway.py:1029-1046
     (`test_slurm_error_codes_align_with_retry_sets`): untouched —
     `SLURM_JOB_FAILED` remains outside both lists.
6. **Deliberately-unknown remainder recorded** (extended per
   fixture-review P2-1's reachability audit):
   - `FORECAST_TASK_{FAILED,CANCELLED}` (closed 2-member family,
     chain_forecast_execution.py:730-735; written to hydro-run
     status, not job rows; CANCELLED's non-transiency is dubious);
   - `NO_ACTIVE_BASINS` (configuration condition, does land on job
     rows);
   - `PUBLISH_TILES_FAILED` (chain_stage_execution.py:746-762 — the
     publish stage's REAL pipeline-job code; its transiency
     semantics were not part of the user ruling, left for a future
     explicit ruling);
   - the ~82-code unclassified long tail (adapters, copyback,
     reconcile families).
   All stay on neither list per the user ruling's boundary. They
   keep the generic unknown scenario's reason + warning. Recorded
   here and in the PR body — NOT in the spec (the unknown scenario
   already covers them; naming them would freeze semantics nobody
   ruled on).
7. **Exemplar-rot repair (fixture-review P1-1)**: classifying
   `PARSE_FAILED` falsifies two live scenarios citing it as an
   unknown-default exemplar — job-retry-mechanism "Pre-Guard
   Evidence Channels Consult Permanence" (downstream-resume
   scenario, live :1272-1281) and multibasin-state-idempotency
   "Resumable downstream failures" (:40). Both MODIFIED in this
   change: `PARSE_FAILED` moves to the non-transient side of the
   exemplar sentence with a "since the stage-failure family joined
   the non-transient list" attribution; `SLURM_JOB_FAILED` (a
   signed #1419 ruling, stable) remains the sole unknown-default
   exemplar. Wording-only: the refusal behavior is identical for
   both categories (`_downstream_failure_restartable` returns
   `not permanent` for any recorded code). Final grep obligation:
   no live spec cites any of the 13 codes as an unknown-default
   exemplar after archive.
8. **Supersession record**: issue items (b)/(c) are closed as
   superseded by #1419 D2 (see proposal); the PR body and the issue
   close-out comment must say so explicitly so the issue's (b)/(c)
   acceptance lines have a recorded disposition instead of silence.
9. **Live-spec parity IN THIS PR (implementation-time ruling)**:
   the suite's parser (`_JOB_RETRY_SPEC_PATH`, tests/test_retry.py:42)
   reads the LIVE spec, and deltas only land at post-merge archive —
   so with delta-only spec changes the reconciliation test
   (:118, code−spec == 3-member frozenset) HARD-FAILS in-PR and the
   spec-driven parametrizes cannot grow. Precedent check: no prior PR
   ever changed the parsed membership (#1459 introduced the parser
   against already-live text; #1419 never touched NON_TRANSIENT), so
   this is the first change needing lockstep. Ruling: the three
   MODIFIED requirements are applied to the LIVE specs
   (openspec/specs/job-retry-mechanism/spec.md,
   openspec/specs/multibasin-state-idempotency/spec.md) within this
   PR, byte-identical to the delta blocks; the deltas stay for
   openspec bookkeeping, and `openspec archive` at chore time becomes
   an idempotent same-content replace (verified then via difflib:
   archive must produce zero live-spec diff). Live-spec edits are
   orchestrator-owned, consistent with the openspec-edit boundary.
   REBASE-FRESHNESS OBLIGATION (round-1 CAND-1, CONFIRMED): the
   byte-identity is against a moving target — git reports NO
   conflict when master edits the same requirement blocks (proven:
   #1420 advanced both blocks mid-PR; merge-tree was clean while
   archive would have deleted #1420's normative text). Therefore
   the 3/3 difflib byte-identity check MUST be re-run after every
   merge/rebase of master into this branch, and once more at chore
   time before `openspec archive`.

## Must preserve

- Retry BEHAVIOR byte-identical: permanence, backoff, resume gates —
  no production file outside retry.py's set construction changes.
- The two now-false test docstrings are REWRITTEN, not left stale
  (fixture-review P2-3): tests/test_retry.py:104-113 ("six
  codes"/"three codes" counts) and :409-421 (the
  SHUD_FAILED-is-unclassified ruling record) — these docstrings ARE
  the recorded rulings for #1314/#1419, and leaving them false
  re-creates the ruling-drift they exist to prevent.
- tests/test_file_orchestration_journal.py's two spec-driven
  parametrized tests (:9184/:9247) grow 6 → 19 each and must be in
  the local evidence run (fixture-review P2-2).
- #1419 pins green unmodified: dual-absence
  (test_real_slurm_gateway.py:1039-1040), classifier explicit
  branch, TRANSIENT∩NON_TRANSIENT=∅, dual-transient-face equality.
- The reason-literal grep pin (tests/test_retry.py ~:352,
  literal-count==1 over services/) — our change adds no new reason
  literal occurrences in services/.
- Existing tests: anchor swaps per decision 5 only; everything else
  unmodified.

## Seams under test

Existing: `_store()`/`_create_job` end-to-end harness (spec-driven
parametrize picks up new codes automatically); the spec-parsing
helper; the reconciliation test. New: the derivation pin (import
identity + set inclusion). No new injectable seams needed.

## Test plan (maps to acceptance)

1. Each of the 13 codes: end-to-end permanently_failed + reason
   `non_transient_error` + zero warning (auto via spec-driven
   parametrize — verify the count grew by 13).
2. `SLURM_JOB_FAILED`: still unknown reason + warning (swapped
   anchor tuple).
3. Derivation pin: stage family ⊆ NON_TRANSIENT_ERROR_CODES with the
   canonical constant as source.
4. Reconciliation + dual-absence + disjointness pins green
   unmodified.
5. `uv run pytest -q tests/test_retry.py
   tests/test_real_slurm_gateway.py` green; ruff tracked clean;
   openspec validate strict.

## Risks to watch

- Do NOT extend `failure_classifier` (out of scope; SHUD family
  already classified; CONVERT/FORECAST/STATE_SAVE_QC/COPYBACK
  _FAILED keep `unknown_failure` classifier strings — classifier is
  a separate surface from the reason lists).
- The spec bullet descriptions must not promise operator remedies
  this change does not deliver — one-line factual rationales only.
- Red evidence needs no tracked mutation: the spec-driven
  parametrize's pre-change run (13 codes absent → parser returns
  old list) vs post-change run is the natural red/green; the
  derivation pin's red is a constructed-set probe.
