# Oracles for two unguarded bounded-evidence last-line invariants (#1171)

## Why

Issue #1171's read-only mutation experiments (branch HEAD 8183667c, three
consuming suites) proved two "last line of defence" semantics in
`services/orchestrator/scheduler_evidence_payload.py` have ZERO oracle:

1. Terminal limit compaction (`_compact_limit`, ~`:314-315`, called at
   ~`:166`) keeps only `reason` — mutating it to keep NOTHING
   (`_compact_mapping(value, ())`) survived 1388 passed / 2 skipped. A
   counting probe showed the terminal layer runs 8 times with `reason`
   present in-input on those suites — the path is exercised, its OUTPUT is
   unasserted. `limit.reason` is the ONLY truncation marker left on a
   deeply-degraded artifact; losing it makes "this evidence was always
   thin" and "this was cut by the size cap" indistinguishable mid-incident.
2. The hard byte bound (`_serialize_evidence_json_if_within_limit`,
   ~`:31`, `serialized_bytes > max_evidence_bytes`) has no off-by-one
   oracle: `> max_evidence_bytes + 1` also survived. The "never exceeds
   the cap" contract rests on one unwatched inequality.

Production logic is CORRECT on both counts (per the issue and re-verified
at fixture time); this change is test-only.

## What Changes

Two boundary tests in `tests/test_production_scheduler.py` (the bounded
evidence cluster ~`:9480-10300`):

1. A payload that provably penetrates to the TERMINAL limit-compaction
   layer (existing construction helpers), asserting the returned
   `limit["reason"] == "evidence_size_limit_exceeded"` survives — kills
   the keep-nothing mutant.
2. An exact-boundary pair on `_serialize_evidence_json_if_within_limit`
   (the shared base of `_payload_fits` and
   `_serialized_evidence_within_limit`): a payload serializing to EXACTLY
   `max_evidence_bytes` bytes MUST pass; EXACTLY `max_evidence_bytes + 1`
   MUST be refused — pins both sides, so both the `+ 1` widening and a
   `>=` narrowing go red.

Spec: ADDED requirement in `runtime-evidence-and-operations` pinning both
invariants as mandatory regression coverage (mirrors the
test-retention-drop-guard-negative-oracle precedent). NO production-code
change.

## Non-goals

- Production edits to `scheduler_evidence_payload.py`; degradation-tier
  redesign; #1168/#1169 fallback semantics (already covered).
- #1172's never-downgrade guard (merged, PR #1234).
- Sweeping all `_compact_*` keep-key sets (issue notes the sibling
  `_compact_review_contract` class but does not require clearing it).
- Mutation-testing tooling (mutmut/cosmic-ray) — separate discussion.
