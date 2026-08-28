## 1. Contract Baseline

- [x] 1.1 Capture the pre-split owner/facade imports, module symbol surface,
  `ProductionSchedulerConfig` signature/dataclass fields/defaults/methods, and
  representative direct/env construction plus DB-free preflight outputs.
- [x] 1.2 Prove the existing focused config, path-safety, direct-owner import, CLI
  propagation, and scheduler-timing tests pass before the move.

## 2. Physical Package Split

- [x] 2.1 Replace `scheduler_config.py` with a package whose `config.py`,
  `path_modes.py`, and `db_free.py` follow existing dependency closures and each
  remain below 1,000 lines.
- [x] 2.2 Re-export `ProductionSchedulerConfig` and all historical module-level
  helper attributes from `scheduler_config/__init__.py` without changing caller
  imports, callback lookup, signature, dataclass fields, defaults, normalization,
  exceptions, or evidence payloads.
- [x] 2.3 Update `_functions_calling_resolve` and
  `test_db_free_normalization_modules_call_resolve_only_where_allowlisted` to scan
  one module file or every `.py` beneath an owner package. The green oracle SHALL
  report scheduler-config callers exactly `{_safe_preserve_final_component}` and
  retry callers exactly `set()`. Temporarily add an unallowlisted `.resolve()` to
  non-barrel `path_modes.py` or `db_free.py`; the named test SHALL fail, after
  which the mutation is reverted and never committed.

## 3. Guard and Governance

- [x] 3.1 Remove only `services/orchestrator/scheduler_config.py` from
  `.large-file-guard.json`; leave the `tests/test_retention.py` exclusion and all
  other threshold/exclusion entries unchanged.
- [x] 3.2 Update `SCHEDULER_COMPATIBILITY_INVENTORY.md` to describe the owner
  package and its preserved import/verification contract.
- [x] 3.3 Confirm every changed/new non-excluded file is below 1,000 lines and the
  entropy/large-file guard accepts a commit touching the new owner package.

## 4. Evidence Floor

- [x] 4.1 Run the focused `tests/test_production_scheduler.py` config/path/DB-free
  selections and all of `tests/test_scheduler_timing.py`; expect zero failures.
- [x] 4.2 Run the scheduler-config compatibility inventory command; expect zero
  failures and unchanged import/callback behavior.
- [x] 4.3 Run `uv run pytest -q tests/`; expect the complete repository test suite
  to pass with no changed test names or semantics.
- [x] 4.4 Run `uv run ruff check .`, the large-file guard/entropy tests,
  `uv run python scripts/governance/audit_repo_entropy.py`, and `git diff --check`;
  expect zero new violations.
- [x] 4.5 Run `openspec validate split-scheduler-config-package --strict
  --no-interactive`; expect strict validation success.
- [x] 4.6 Compare the captured pre/post symbol, signature, dataclass and
  representative output snapshots; expect semantic equality apart from physical
  module ownership, and report any plan deviation explicitly.
