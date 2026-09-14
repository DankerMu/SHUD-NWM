## Why

`evaluate_canonical_readiness` (`workers/canonical_converter/converter.py`) reports
`canonical_identity_mismatch` for a fresh cycle whose canonical product set is
empty (#2042). With `products=[]` the observed policy/object identity sets are
empty by construction, so the predicate `expected_policy_id and not policy_identities`
fires and the reason branch falls through to `canonical_identity_mismatch` even
though `candidate_row_count == 0` and `identity_rejected_row_count == 0`. Production
GFS cycle `gfs_2026090312` froze this false identity conflict into six Yellow River
rows of a forecast index; operators triaging by typed reason read "canonical not
yet ingested" as "lineage conflict, needs manual repair".

## What Changes

- Zero candidate rows never set `identity_mismatch`: an empty product set cannot
  mismatch an identity. The reason falls through to the existing
  `missing_canonical_variables` (resolved implementation choice — no new reason
  string, no consumer keys on it). `status`, `ready`, counts and
  `missing_variables`/`missing_leads` are unchanged.
- Rows present with mismatching policy/source-object identity, or partially
  missing lineage, keep `canonical_identity_mismatch` / `canonical_lineage_missing`
  byte-for-byte.
- `docs/runbooks/scheduler-dbfree-typed-reasons.md` gains a section stating that
  `canonical_identity_mismatch` means rows exist and their identity disagrees; a
  zero-row fresh cycle reports `missing_canonical_variables`.

Fixture level `compact`: `design.md` is exempt (one predicate narrowed, no new
contract, format, path or state transition).

## Impact

- Code: `workers/canonical_converter/converter.py` only.
- Tests: `tests/test_canonical_converter.py`, `tests/test_production_scheduler.py`
  (`_fresh_zero_row_readiness_provider` starts asserting the reason).
- Unchanged: fresh-zero-row admission (`_canonical_evidence_is_fresh_zero_row` in
  `services/orchestrator/scheduler_candidates.py`), the file-provider
  `canonical_identity_mismatch_cache_miss` path, forecast index projection in
  `services/slurm_gateway/real_backend.py`, and already-frozen node-22 index files.
