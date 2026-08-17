# Tasks: retry-stage-failure-classification

## 1. Classification set (#1462 item (a))

- [x] 1.1 `NON_TRANSIENT_ERROR_CODES` (services/orchestrator/
      retry.py:40): add the SHUD trio + 3 task codes as literals
      (one-line comments) and the 7-stage family DERIVED from
      `DOWNSTREAM_RESTART_STAGES` (cycle-free import from
      `scheduler_state_types`); comment names this change, the
      derivation, and the #1419 dual-absence constraint
      (`SLURM_JOB_FAILED` must stay out).
- [x] 1.2 Prove behavior invariance on the final head: grep shows
      `NON_TRANSIENT_ERROR_CODES`'s only production consumer is the
      reason ternary (retry.py:150); `is_retryable_failure` reads
      the transient list only; recorded in the PR body.

## 2. Spec delta

- [x] 2.1 13 codes join the "Non-transient error codes block
      auto-retry" bullet list in parser-compatible format
      (`- \`CODE\` — rationale`); new "Stage-failure codes track the
      canonical downstream stage domain" scenario (outside the
      parser window — verify the parser still returns exactly the
      first scenario's list); byte-faithful otherwise (difflib).
- [x] 2.2 Exemplar-rot repair (P1-1): MODIFIED "Pre-Guard Evidence
      Channels Consult Permanence" (job-retry-mechanism, downstream-
      resume scenario exemplar swap) + MODIFIED "Resumable
      downstream failures" (multibasin-state-idempotency:40, same
      swap); wording-only, byte-faithful otherwise; final grep
      proves no live spec still cites any of the 13 codes as an
      unknown-default exemplar.

## 3. Test anchors (swap, never delete)

- [x] 3.1 `_UNLISTED_PRODUCTION_ERROR_CODES`
      (tests/test_retry.py:56) → `("SLURM_JOB_FAILED",)`; comment
      records SHUD_FAILED's move to the classified list (#1462).
      The unknown-branch pin keeps running for SLURM_JOB_FAILED
      (unknown reason + warning preserved — #1419 D2 stands).
      REWRITE both now-false docstrings (P2-3): :104-113 ("six
      codes"/"three codes" → the new counts) and :409-421 (the
      SHUD_FAILED-is-unclassified sentences → record that #1462
      classified it here and the test now guards the #1419 ruling
      alone).
- [x] 3.2 New derivation pin: the stage family
      `{f"{s.upper()}_FAILED" for s in DOWNSTREAM_RESTART_STAGES}`
      ⊆ `NON_TRANSIENT_ERROR_CODES`, plus
      `retry.DOWNSTREAM_RESTART_STAGES is
      scheduler_state_types.DOWNSTREAM_RESTART_STAGES` (proves the
      import edge exists — Note-1: a runtime assert cannot
      distinguish derivation from a hand copy, so the pin claims
      inclusion + import edge, no more); red evidence via
      constructed-set probe (in-memory).
- [x] 3.3 Verify the spec-driven parametrize
      (`_spec_non_transient_error_codes`) grew by exactly 13 and
      each new code passes the full end-to-end pin
      (permanently_failed + reason `non_transient_error` + zero
      warning) — in tests/test_retry.py AND in the two file-journal
      production points (tests/test_file_orchestration_journal.py
      :9184/:9247, each 6 → 19 cases — P2-2).
- [x] 3.4 #1419 pins green UNMODIFIED: dual-absence
      (test_real_slurm_gateway.py:1039-1040), classifier branch,
      TRANSIENT∩NON_TRANSIENT=∅, dual-transient-face equality,
      reason-literal grep pin.

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_retry.py
      tests/test_real_slurm_gateway.py
      tests/test_file_orchestration_journal.py` green (P2-2 added
      the third file; counts before/after recorded — test_retry
      125 → 151 measured (fixture-review Note-2 predicted 150; the
      −1 from the unlisted-codes parametrize shrinking 2→1 offsets
      against the +1 new derivation pin), file-journal +26; the spec-driven parametrize's growth is the structural
      red/green).
- [x] 4.2 `git ls-files '*.py' | xargs uv run ruff check` clean.
- [x] 4.3 `openspec validate retry-stage-failure-classification
      --strict --no-interactive` valid; difflib per block: guard
      requirement = 13 bullets + 1 scenario; the two exemplar-swap
      requirements = wording-only swaps; zero other changes.
- [x] 4.4 Diff = retry.py + tests/test_retry.py + the two-capability
      spec deltas + the byte-identical LIVE spec parity edits
      (design decision 9: openspec/specs/job-retry-mechanism/spec.md,
      openspec/specs/multibasin-state-idempotency/spec.md); no other
      production file.
- [x] 4.5 Issue #1462 acceptance mapped in the PR body, including
      the (b)/(c) supersession record (#1419 D2) and the
      deliberately-unknown remainder (FORECAST_TASK_*,
      NO_ACTIVE_BASINS).
