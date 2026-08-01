# Tasks: test-bounded-candidate-lists-never-downgrade

Fixture level: compact · Repair intensity: light · Issue #1172

Triage note: test-only oracle addition for a correct, unguarded
production guard. Risk axes: (1) the new test must genuinely kill the
issue's mutant (not pass vacuously via an unreachable setup), (2) it
must drive the REAL summarize tier (injected seam or direct call), not
re-implement the guard, (3) spec clause must not contradict the
existing `:85` half. Single review round.

Must preserve:
- `services/orchestrator/scheduler_evidence_payload.py` byte-identical
  (test-only change)
- Existing bounded-evidence suites green: baseline 1409 passed +
  2 skipped (post-#1178; the issue's 1389 figure predates it) across tests/test_production_scheduler.py,
  tests/test_production_readiness_validation.py,
  tests/test_scheduler_timing.py

Must add:
- One test asserting `limit["candidate_lists"] == "dropped"` survives
  the summarize tier when the three candidate lists are already `[]`
  (constructed next to the injected-seam scaffolding of
  `test_fit_summary_tier_summarizes_injected_unsummarized_bounded_payload`
  ~`:9626`, or via the direct-driver template
  `test_bounded_evidence_summary_rows_are_idempotent_under_a_second_fallback`
  ~`:9599`, with the marker pre-set — anchor by function name, lines
  drift)
- Spec delta clause: marker monotone, never downgraded

## Implementation tasks

- [ ] 1. New test per proposal; name states the invariant (e.g.
  `test_summarize_tier_never_downgrades_dropped_candidate_lists`).
- [ ] 2. Red proof: mutate the guard to `if summarized:` on a scratch
  copy → new test fails; seam-liveness sanity (unmutated baseline
  green). Record both outputs.
- [ ] 3. Oracle: `uv run pytest -q tests/test_production_scheduler.py
  tests/test_production_readiness_validation.py
  tests/test_scheduler_timing.py` green; `uv run ruff check .` clean;
  `openspec validate test-bounded-candidate-lists-never-downgrade
  --strict --no-interactive` valid.

## Required evidence

- Red-proof mutation output (new test failing under the cut guard)
- Baseline suite counts (expect 1410 passed + 2 skipped)
- ruff + openspec validate outputs

## Non-goals

- Production-logic edits; #1171; mutation tooling; property tests
