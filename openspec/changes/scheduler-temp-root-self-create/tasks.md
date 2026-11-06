# Tasks: scheduler-temp-root-self-create

## 1. Preflight + creation

- [x] 1.1 Admit a missing `temp_root` on the runtime-root preflight's
  `allow_create` arm; keep `runtime_root` and every other root `must_exist`.
  Evidence floor: `tests/test_production_scheduler.py::test_no_flag_invalid_env_roots_block_before_registry_adapter_or_submit`
  still blocks workspace/object-store/runtime/lock/evidence roots with
  `SCHEDULER_ROOT_<FIELD>_NOT_FOUND`.
- [x] 1.2 Create the admitted `temp_root` (no-follow parent pin, final
  component only, mode 0700) in both `run_once` runtime-root preflight sites
  (db-free pre-lock, db-backed post-lock), before the `blocked` branch; record
  `checks.temp_root.created`; a creation failure blocks with
  `SCHEDULER_ROOT_TEMP_ROOT_<REASON>`.
  Evidence floor: `test_no_flag_missing_temp_root_is_created_0700_and_the_pass_proceeds`,
  `test_db_free_pass_creates_missing_temp_root_with_redacted_created_evidence`,
  `test_temp_root_creation_race_outcomes_fail_closed_through_the_preflight_verdict`,
  `test_temp_root_creation_acts_only_on_a_ready_creatable_missing_verdict` pass;
  the first two are red on the pre-change source.
- [x] 1.3 Refusals keep their codes and create nothing (parent missing,
  outside approved roots, parent symlink out of approved roots, dangling
  symlink, parent swapped to a symlink after config).
  Evidence floor: `test_no_flag_refused_missing_temp_root_still_blocks_and_creates_nothing`,
  `test_temp_root_whose_parent_became_a_symlink_after_config_is_refused_and_not_created` pass.
- [x] 1.4 Bounded evidence keeps `checks.temp_root.created`.
  Evidence floor: `test_bounded_evidence_keeps_the_temp_root_created_proof` passes.

## 2. Docs + verification floor

- [x] 2.1 `docs/runbooks/two-node-production-e2e-plan.md` states the temp_root
  self-create rule and the unchanged fail-closed roots.
- [x] 2.2 `openspec validate scheduler-temp-root-self-create --strict --no-interactive` PASS.
- [x] 2.3 `uv run ruff check .` PASS; `uv run pytest -q tests/test_any_ratchet.py` PASS.
- [x] 2.4 Selector-routed pytest files for the changed sources PASS locally.
- [ ] 2.5 node-22 live acceptance (deferred to the post-catch-up node-22 sync
  window; node-22's 3.12 venv is never `uv sync`ed): delete the temp root, run
  one pass, observe no `preflight_blocked` and `created=true` in evidence.
