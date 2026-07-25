# Tasks: pin-identity-equality-none-semantics

Fixture level: compact (tests-only, single file, no production-code change;
issue is implementation-ready with enumerated scenarios)
Repair intensity: normal

Risk packs considered (core):
- Public API / CLI / script entry: not selected - no runtime surface touched
- Schema / columns / units / field names: not selected - identity field lists
  frozen (out of scope by issue)
- Legacy compatibility / examples: selected - existing 40+ `_registry_row`
  call sites and the full suite must pass unmodified
- Error handling / rollback / partial outputs: not selected - pure predicate
  under test
- Test-oracle integrity: selected - the new tests must be mutation-capable
  (able to go red), not tautological

## 1. Regression tests

- [ ] 1.1 Add parametrized unit tests in
  `tests/test_scheduler_file_provider_refresh.py` calling
  `refresh._rows_have_identical_identity` directly with minimal dicts
  (do NOT extend `_registry_row`):
  (a) flat field (e.g. `lifecycle_state`) both sides `None` → `True`;
  (b) nested `resource_profile.source_inventory_checksum` both sides `None`
  → `True`;
  (c) both sides missing top-level `resource_profile` → `True`;
  (d) asymmetric flat: `segment_count` `0` vs `None`, and `lifecycle_state`
  `""` vs `None` → `False` (falsy non-None values, chosen to kill the
  truthiness mutation);
  (e) asymmetric nested: one side missing the top-level `resource_profile`
  key entirely, other side carrying
  `{"resource_profile": {"source_inventory_checksum": None}}` → `False`
  (sentinel != None — pins the missing-vs-explicit-null distinction);
  (f) flat missing-vs-explicit-None: one side omits `lifecycle_state`, the
  other carries `lifecycle_state: None` → `True` (`dict.get()` collapses
  both to `None`; pins the flat side against a sentinel-based rewrite that
  would flip this to `False` with CI green).
  Minimal dicts must still include both compared sides' remaining identity
  fields equal (or equally absent) so only the scenario field drives the
  verdict.
  Evidence floor: all new tests green on head;
  `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` full-suite
  green; `uv run ruff check .` clean.
- [ ] 1.2 MANDATORY mutation-check: locally mutate the flat compare to
  `bool(row.get(field_name)) != bool(previous_row.get(field_name))`
  (`scripts/scheduler_file_provider_refresh.py:2452-2454`) → scenario (d)
  must go red; restore the original line and re-run green. Record both
  outputs verbatim in the report/PR body. Production file must be
  byte-identical after restore
  (`git diff --stat -- scripts/scheduler_file_provider_refresh.py` empty).

## 2. Change-level verification floor

- [ ] 2.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
  green (full suite, not just new tests).
- [ ] 2.2 `uv run ruff check .` clean.
- [ ] 2.3 `openspec validate pin-identity-equality-none-semantics --strict
  --no-interactive` PASS.
- [ ] 2.4 Zero production-code change (issue acceptance criterion 5):
  `git diff --name-only origin/master...HEAD -- scripts/ packages/ apps/
  services/ workers/` is empty; outside `openspec/` the only changed file is
  `tests/test_scheduler_file_provider_refresh.py`.
