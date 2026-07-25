# Tasks: enforce-bootstrap-reconciliation-sum

Fixture level: compact (1-line fail-closed validator addition + 3 negative
tests; issue is implementation-ready with the exact patch shape enumerated)
Repair intensity: normal

Risk packs considered (core):
- Error handling / rollback / partial outputs: selected - the change IS a
  new raise branch; must be fail-closed for tampered receipts and invisible
  for every honest writer
- Legacy compatibility / examples: selected - existing bootstrap happy-path
  receipts (all buckets empty) must keep validating; full suite green
  unmodified
- Schema / columns / units / field names: not selected - receipt schema and
  field semantics untouched (the rejected previous_count==0 alternative
  would have needed this; declined)
- Public API / CLI / script entry: not selected - internal validator helper,
  single definition, no signature change
- Test-oracle integrity: selected - negative tests must be red on pre-change
  code (true red-proof, not vacuous)

## 1. Validator + regression tests

- [ ] 1.1 `scripts/scheduler_file_provider_refresh.py`, inside the
  `previous_sha is None` branch (`:1995-2000`), after the existing
  `if previous_count is not None: raise`, add the dual equality:
  `if unchanged_total + package_changed_total + removed_total != 0:` →
  `raise ValueError("receipt_classification_invalid")`, with a short
  comment stating bootstrap semantics (no previous registry → nothing can
  be unchanged/package_changed/removed). No other branch or function
  touched.
  Evidence floor: `uv run ruff check .` clean; full suite green (task 2.1).
- [ ] 1.2 Three negative receipt-tampering tests in
  `tests/test_scheduler_file_provider_refresh.py`, mirroring the
  validator-negative pattern at `:3538` (build a bootstrap receipt with
  `previous_registry_sha256=None`, `previous_model_count=None`). The
  outcome/reason configuration per variant is LOAD-BEARING (fixture-review
  verified pre=PASS/post=RAISE for exactly these; other natural pairings
  are already rejected pre-change by ADJACENT checks and would make the
  red-proof vacuous):
  (a) non-empty `removed` (total 1) → `outcome="failed"`,
  `reason="registry_cutover_removal_refused"`, `refused` covering the
  removed entry (MUST be a refusal outcome — `published` requires
  `refused==0` at `:2024-2028`);
  (b) non-empty `unchanged` (total 1) → `outcome="published"`,
  `reason="success"`, `refused` total 0, `prospective_model_count` adjusted
  so added+unchanged+package_changed still equals it (MUST be a non-refusal
  reason — refusal reasons require `refused>=1` at `:2030-2032`); carry the
  full provider triple `["registry","readiness","state"]` (`:1729-1733`)
  or the receipt fails `receipt_provider_invalid` first;
  (c) non-empty `package_changed` (total 1) → `outcome="failed"`,
  `reason="registry_cutover_undeclared"`, `refused` covering it.
  Each asserts `pytest.raises(ValueError)` with
  `"receipt_classification_invalid"`. Do NOT add jsonschema assertions —
  the schema encodes no reconciliation equalities, so there is no
  schema/runtime symmetry to pin here.
  Evidence floor: RED-PROOF mandatory — run all 3 against pre-change code
  (e.g. stash the production hunk or run from a master worktree):
  all 3 must FAIL (receipt validates clean); green after the 1.1 line.
  Record both outputs verbatim.
- [ ] 1.3 Bootstrap happy-path regression (fixture-review pre-identified;
  re-run and confirm green post-change): the `_classification_stub()`
  helper (`tests/test_scheduler_file_provider_refresh.py:457`) is bootstrap
  -shaped (null sha/count, empty buckets) and flows through
  `_validate_receipt` in
  `test_receipt_schema_and_runtime_reject_same_expressible_negative_corpus`
  (`:1751`), `test_publish_primary_receipt_upgrades_over_pre_1080_latest`
  (`:3763`), and `test_publish_primary_receipt_replaces_corrupt_latest`
  (`:4409`). Do NOT cite
  `test_cutover_gate_missing_previous_canonical_is_first_publication`
  (`:3123`) as coverage — it exercises the precommit-gate sink only and
  never reaches `_validate_receipt`.
  Evidence floor: the three named tests green post-change.

## 2. Change-level verification floor

- [ ] 2.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
  green (full suite).
- [ ] 2.2 `uv run ruff check .` clean.
- [ ] 2.3 `openspec validate enforce-bootstrap-reconciliation-sum --strict
  --no-interactive` PASS.
- [ ] 2.4 Scope check: production diff confined to the bootstrap branch of
  `_enforce_registry_classification_reconciliation`
  (`git diff origin/master -- scripts/scheduler_file_provider_refresh.py`
  shows exactly one hunk there); no schema, no other production file.
