# Design: delete-zero-reference-dead-code (#3147)

Change surface: 40 zero-reference helpers across `packages/common`, `services/orchestrator`,
`services/production_closure`, `services/tile_publisher`, `workers/*`, `apps/api` and `scripts/`; 9 forwarders in
`services/orchestrator/scheduler.py:479-554`; `scripts/m24_grib_preflight_proof.py`; the rows of
`docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` that name the forwarders.

Must preserve:
- Runtime behaviour. Every touched module still imports, and every existing test passes unmodified.
- The forwarders that stay alive. Each is called back into the facade from an owner module as `_scheduler.<name>`:
  - `_fetch_active_model_details` (`scheduler_models.py:66`)
  - `_active_model_duplicate_exclusions` (`:69`)
  - `_coerce_registered_model` (`:76`, plus 8 test files)
  - `_matches_filters` (`:86`)
  - `_filter_expression` (`:112`)
  - `_mapping_value` (`scheduler_candidate_quality.py:112`)
  - `_coerce_output_segment_count` (`scheduler_candidate_quality.py:281`)
  - `ProductionScheduler._discover_models`
- The `scheduler-model-discovery-forwarders` group row. Inventory rows 131 and 160 only lose the 9 deleted names.
  `tests/test_production_scheduler.py:849` pins the governed group-id tuple.
- The inventory removal condition (row 131). The focused discovery tests must show that pagination limits,
  exclusions, duplicate-identity redaction, segment coercion and filters still behave the same.
- The structural budget and the hard-gate audit report no new findings.

Must add/change: deletions only. The PR records, per symbol, the grep command and its hit count.

Governing invariant: nothing reachable, statically or dynamically, from production code, tests, deployment units, CI
or packaging entry points is deleted.

Sibling surfaces:
- dynamic reach: `getattr`/`importlib` dispatch, owner-module callbacks into the facade (`_scheduler.<name>`),
  `monkeypatch.setattr` targets, `pyproject` `[project.scripts]`, `infra/sbatch`, `infra/systemd`, `.github/`;
- the facade guard (`scripts/governance/entropy_audit/facade_guard.py:537-560`, `:693-725`). It reports only facade
  code that is missing from the inventory, never inventory names whose code is gone. Stale inventory names are
  therefore checked explicitly with a grep;
- the selector tables (`scripts/select_ci_tests*.py`) and living docs that name a deleted symbol or the script.

Seams under test: module import; the scheduler discovery tests; the facade guard and governed-group tests; the
selector tests.

Required evidence: see `tasks.md` §2. Each item lists its command and expected result.

Non-goals: test-only helpers (#3148); retired-route and template branches (#3149); file splits.

Review focus:
- the per-symbol zero-reference evidence, including callbacks and string dispatch;
- the forwarders that are kept;
- inventory rows that are edited but not dropped;
- no test modified.
