# Tasks: cleanup-orphan-execution-audit-validator

Fixture level: compact · Repair intensity: light · Issue #1086

Triage note: S-size dead-code deletion, zero callers for all three
targets (fixture review extended scope from the issue's one function to
the full aace0913 orphan family — recorded deviation). Risk axes:
(1) deletions must not orphan a shared helper (each referenced helper
keeps ≥1 other caller — re-verify by grep after delete);
(2) `_invocation_execution_identity` loses no in-script caller status it
did not already have and keeps its live test caller
(tests/test_node27_timeseries_compression_live_evidence.py:197) — it
MUST stay; (3) no unused imports left (ruff F401); (4) schema const-pins
untouched; (5) blank-line hygiene at deletion seams (keep exactly two
blank lines between remaining defs). Single review round.

Must preserve:
- `authorization.database_audit_proof` and
  `execution.database_audit_proof` `{"const": false}` at
  schemas/timeseries_compression_live_evidence.schema.json:61,:80.
- Every function not in the three-target list, including
  `_invocation_execution_identity` (:270) and `INVOCATION_ARGV`
  (callers :283/:295 + tests).
- `tests/test_node27_timeseries_compression_live_evidence.py` green
  unmodified (baseline 270 passed; zero references to the three
  targets).

## Implementation tasks

- [x] 1. Delete `_validate_execution_audit` (:648-716),
  `_validate_invocation_record` (:589-645), and `_artifact_refs_in`
  (def at :2916; find its exact end) from
  scripts/node27_timeseries_compression_live_evidence.py, keeping
  two-blank-line separation at each seam.
- [x] 2. Verify no helper became an orphan: `_require_mapping`,
  `_require_exact_keys`, `_parse_utc`, `_require_list`,
  `_text_artifact`, `EvidenceError`, `INVOCATION_ARGV`,
  `_invocation_execution_identity` each retain ≥1 non-definition caller
  (in-file or tests); remove any import made unused (expected: none).
- [x] 3. Oracle: `grep -rn --include="*.py" -e _validate_execution_audit
  -e _validate_invocation_record -e _artifact_refs_in .` → 0 hits;
  `uv run ruff check .`; `uv run pytest -q
  tests/test_node27_timeseries_compression_live_evidence.py` → 270
  passed; `git diff` touches exactly one file; schema file untouched;
  `openspec validate cleanup-orphan-execution-audit-validator --strict
  --no-interactive`.

## Required evidence

- grep zero-hit output; helper-caller counts; pytest + ruff outputs

## Non-goals

- Audit-oracle re-introduction; schema/const changes; unrelated cleanup.
