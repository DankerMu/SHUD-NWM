# Pin the candidate_lists never-downgrade guard with a real oracle (#1172)

## Why

`_summarize_bounded_candidate_lists`'s guard
(`services/orchestrator/scheduler_evidence_payload.py:240`) keeps a
`limit.candidate_lists == "dropped"` marker from being downgraded back to
`"summarized"` by a later summarize pass. The production logic is CORRECT,
but issue #1172's read-only mutation experiment proved NO test defends it:
cutting the `!= "dropped"` half survived the issue's full runs (then
1389 passed / 2 skipped; the suites now sit at 1409 + 2 after PR #1178) across
the three consuming suites (seam-liveness control: a bogus write kills 5).
The branch is unreachable on the default single-pass `_fit` path (summary
tier runs before the droppable tier), reachable only via the injected
bounded-payload seam or a second `_fit` pass — exactly the
incident-readability semantics ("thin" vs "cut") that #1168 introduced the
marker to convey, and exactly what silent deletion would turn into lying
evidence with green CI.

## What Changes

- ONE new test in `tests/test_production_scheduler.py`, mirror of the injected-seam case
  `test_fit_summary_tier_summarizes_injected_unsummarized_bounded_payload`
  (~`:9626`): construct a bounded payload whose
  `limit["candidate_lists"] == "dropped"` with all three candidate lists
  already `[]`, run the summarize tier, assert the marker is STILL
  `"dropped"`. Red-proof: with the guard cut to `if summarized:`, the new
  test MUST fail (mutation run recorded).
- Spec: `runtime-evidence-and-operations` requirement "Bounded evidence
  observability floor" gains the complementary clause in the
  candidate-lists scenario — the marker is monotone; a later summarize
  pass never downgrades `dropped` back to `summarized`.
- NO production-code change (`scheduler_evidence_payload.py` untouched).

## Non-goals

- #1171's two invariants (terminal `_compact_limit` reason loss +
  `MAX_EVIDENCE_BYTES` off-by-one) — different functions, tracked there.
- Mutation-testing tooling; property-based transition-matrix test
  (rejected in the issue as over-engineering for one unguarded clause).
- Any change to `_fit` tier ordering or reachability.
