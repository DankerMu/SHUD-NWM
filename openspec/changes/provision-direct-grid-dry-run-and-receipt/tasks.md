# Tasks

## 1. Implementation

- [x] 1.1 `workers/model_registry/direct_grid_variant_registration.py`: read-only planning function sharing
      validation, snapshot resolution, lookup and id derivation with `register_direct_grid_variant`.
- [x] 1.2 `scripts/provision_direct_grid_scheduler_registry.py`: dry-run default (read-only connection),
      `--apply`, `--succession-id`, `--receipt-root`, `--build-tmp-dir`, temporary build, receipt with
      up-front path checks, per-variant apply-requires-dry-run check inside the transaction.
- [x] 1.3 Tests (fake connection, `tmp_path`) for every item of the proposal's evidence list.
- [x] 1.4 `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`: the script and the registration
      module select the new suite.
- [x] 1.5 Runbooks: add the command sequence to `service-bringup.md` hop 3 and `recalibration-and-archive.md`
      section 5.7.1; no `file.py:NNN` references.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; targeted pytest for the new suite,
      `tests/test_direct_grid_variant_registration.py`, `tests/test_provision_direct_grid_ic_header_gate.py`,
      `tests/test_basins_package.py`, `tests/test_state_clone_baseline_cutover_cli.py`,
      `tests/test_select_ci_tests.py`.
- [ ] 2.2 CI green on the PR.
- [ ] 2.3 node-27 after merge: one real dry-run for an already-provisioned model, receipt root in a
      throwaway directory under `/home/nwm/tmp`, `export TMPDIR=/home/nwm/tmp`; it predicts the existing
      `model_id`, `inserted = false`, and `git status` / the object store / the database are untouched. No
      `--apply`.
