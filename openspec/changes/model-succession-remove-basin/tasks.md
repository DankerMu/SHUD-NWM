# Tasks

## 1. Implementation

- [x] 1.1 `--kind remove_basin`, `--remove`, argument rules by kind, `Plan.removes`, plan record.
- [x] 1.2 Inputs without a provision receipt; `check_inputs` for this kind (order and predicate unchanged).
- [x] 1.3 `preflight` / `publish` / `preview` with `Operations(remove=...)`; continuity; dry-run and abort
      texts.
- [x] 1.4 Helpers and the suite `tests/test_node22_model_succession_remove_basin.py`.
- [x] 1.5 `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`: the new suite on the existing rules.
- [x] 1.6 Runbooks: `recalibration-and-archive.md` 5.7.4; pointer in `operating-scope.md` 7.2.

## 2. Evidence Floor

- [ ] 2.1 Local: `uv run ruff check .`; the new suite; the five existing succession suites; the publish-tool
      suites; `tests/test_node22_entrypoint_invariant.py`; `tests/test_entropy_audit_line_references.py`;
      `tests/test_select_ci_tests.py` (after staging, last);
      `openspec validate model-succession-remove-basin --strict --no-interactive`.
- [ ] 2.2 CI green on the PR.
- [ ] 2.3 node-22 after merge and pull: one `--kind remove_basin` dry-run with the exact interpreter and a
      throwaway `--receipt-root`, naming both models of one production basin; nothing changes.

Deviation: node-27 真实 DB receipt 待链路恢复后补. This change is DB-free; pending command:
`cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH TMPDIR=/home/nwm/tmp && uv run --no-sync pytest -q
tests/test_node22_model_succession*.py`. Local results are not a node-27 PASS. No production `--apply`.
