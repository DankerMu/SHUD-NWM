# Tasks: test-bounded-evidence-invariant-oracles

Fixture level: compact · Repair intensity: light · Issue #1171

Triage note: test-only oracle addition for two correct-but-unguarded
last-line invariants. Risk axes: (1) the terminal-layer test must
PROVABLY reach the terminal compaction (not stop at an earlier tier —
the issue infers existing `limit.reason` assertions never penetrate or
never re-assert), (2) the byte-boundary test must control serialized
size EXACTLY (json separators/key order — construct by measuring, not
by guessing), (3) both issue mutants plus the `>=` narrowing must go
red. Single review round.

Must preserve:
- `services/orchestrator/scheduler_evidence_payload.py` byte-identical
- Baseline suites green: tests/test_production_scheduler.py,
  tests/test_production_readiness_validation.py,
  tests/test_scheduler_timing.py (1410 passed + 2 skipped at branch
  time — recount at HEAD before recording)

Must add:
- Terminal-layer test asserting `limit["reason"] ==
  "evidence_size_limit_exceeded"` on a payload that penetrates to the
  terminal compaction, with a construction-side proof it actually got
  there (e.g. assert the compacted `limit` block lost its other keys —
  the terminal layer's own signature — so the test cannot silently
  pass at an earlier tier)
- Exact-boundary pair driving `_serialize_evidence_json_if_within_limit`
  (or `_payload_fits`) at `== max_evidence_bytes` (accept) and
  `== max_evidence_bytes + 1` (refuse), sizes asserted by measuring
  `len()` of the actual serialization inside the test

## Implementation tasks

- [x] 1. Two tests per proposal, placed in the bounded-evidence cluster,
  names stating the invariants.
- [x] 2. Red proof (scratch copy, no git stash): (a) `_compact_limit` →
  `_compact_mapping(value, ())` → terminal test fails; (b) bound `>` →
  `> max_evidence_bytes + 1` → boundary test fails; (c) bound `>` →
  `>=` → boundary test fails (lower side pinned). Record outputs.
- [x] 3. Oracle: three suites green + `uv run ruff check .` +
  `openspec validate test-bounded-evidence-invariant-oracles --strict
  --no-interactive`.

## Required evidence

- Red-proof outputs for mutants (a)/(b)/(c)
- Baseline suite counts
- ruff + openspec validate outputs

## Non-goals

- Production edits; `_compact_*` sweep; mutation tooling; #1172 (merged)
