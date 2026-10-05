# Tasks

## 1. Implementation

- [x] 1.1 Importable merge-and-publish function plus `scripts/node22_publish_merged_scheduler_registry.py`
      (dry-run default, `--apply`, `--replace` / `--add` / `--remove`, `--succession-id`, `--receipt-root`,
      `--new-rows-registry`).
- [x] 1.2 Checks, backups, canonical-then-mirror publish with a shared `generated_at`, read-back, rollback.
- [x] 1.3 Receipts; generic receipt helpers shared with the provision step, not copied.
- [x] 1.4 Tests for every item of the proposal's evidence list.
- [x] 1.5 `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`: the tool and its module select the
      new suite; the shared receipt helpers select both the provision suite and the new suite; node-22 entrypoint guards updated if the new script or runbook text falls under them.
- [x] 1.6 Runbooks: `recalibration-and-archive.md` 5.7.1, `service-bringup.md` hop 4, `operating-scope.md`
      7.2 use the tool; no `file.py:NNN` references.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; targeted pytest for the new suite, the provision dry-run suite,
      `tests/test_select_ci_tests.py`, `tests/test_node22_entrypoint_invariant.py`,
      `tests/test_node22_entrypoint_invariant_python_scan.py`,
      `tests/test_entropy_audit_line_references.py`.
- [ ] 2.2 CI green on the PR.
- [ ] 2.3 node-22 after merge and pull: one real dry-run against the production manifests with the exact
      interpreter and a throwaway `--receipt-root`, remove-only for one basin's two rows (needs no provision
      receipt); both manifests keep their sha256 and mtime, no backup appears. No `--apply`.
