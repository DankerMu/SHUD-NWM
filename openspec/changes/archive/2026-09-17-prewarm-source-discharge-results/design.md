## Context
The existing PNG outcome buckets are the pattern; discharge currently increments only discharge_requests.
Change surface: prewarm aggregate and empty source entry; SUMMARY_SCHEMA and consumer tests.
Must preserve: total discharge_requests, status-based 2xx success, failures cap twenty, PNG classification, source discovery isolation, deadline skip exclusion and nonzero exit behavior.
Must preserve: workers=8, timeout=30s, deadline=540s, lead window=12h, z3/z4 discharge scope; no tuning before cold receipt.

## Goals / Non-Goals
Goal: every issued discharge result belongs to exactly one source and one outcome bucket.
Non-goals: cold measurement instrumentation in permanent source, parallelism/timeout/pool/SQL tuning, cold admission, full-axis prewarm or new retry logic.
The cold receipt follows this slice's merge; A and #2346 step two are one later coordinated batch.

## Decisions
Governing invariant: for each source, discharge_requests == discharge_ok + discharge_failed, with no unissued request in any of those counts.
Keep discharge_requests to preserve existing totals; it is a useful total rather than a deprecated alias.
Classify with the existing 200 <= status < 300 rule, before the existing global failure logic.
Initialize both buckets to zero even for discovery failure or empty sources.
Advance schema to v3 rather than silently changing the observable shape under v2.
Sibling surfaces: normal JSON summary, CLI exception envelope, runbook examples and existing test fixtures. PNG classification and cron caller stay unchanged.
Seams under test: prewarm output JSON/return code using existing injected discovery and warmer fixtures; CLI error schema.
Required evidence: gfs mixed successes/failures and ifs with a distinct outcome distribution yield exact separate counters and unchanged global failure counts.
Required evidence: exceptions converted to failed WarmResult count as discharge_failed; deadline-excluded jobs do not count; unavailable sources have zero counters.

## Risks / Trade-offs
Schema consumers may pin v2: search and update current consumers while retaining historical receipts unchanged.
Testing only all-success misses the defect: require a mixed-result regression that fails on base source.
The test oracle is node-27, not a local DB; local lint is not runtime proof.

## Migration Plan
Merge B without closing #2121, sync via GitHub to a clean node-27 verification checkout, then use v3 for the cold receipt.
Rollback is reverting this additive accounting change; no DB or cache migration.
Review focus: source association, skip exclusion, unchanged global failure semantics, schema consumers.
