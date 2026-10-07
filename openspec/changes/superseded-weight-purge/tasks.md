# Tasks

## 1. Implementation

- [x] 1.1 `scripts/node27_purge_superseded_weights.py`: arguments, refusals, manifest, classification, dry-run.
- [x] 1.2 Apply: lock, per-model backup + delete transaction, receipts, pause, cap.
- [x] 1.3 `tests/test_node27_purge_superseded_weights.py` and `…_integration.py`; CI selector: the same-name
      pair is derived automatically — add a rule only if the meta-guards ask for one (integration file).
- [x] 1.4 Runbook section in `docs/runbooks/production-ops/operating-scope.md`.

## 2. Evidence Floor

- [ ] 2.1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_node27_purge_superseded_weights.py`
      (the integration file skips without a disposable database);
      `tests/test_select_ci_tests.py` and the entropy/line guards after staging;
      `openspec validate superseded-weight-purge --strict --no-interactive`.
- [ ] 2.2 CI green on the PR; the real-DB lane log shows the integration file PASSED, not skipped.
- [ ] 2.3 node-27 after merge: dry-run output (read-only) recorded as evidence.

Deviation: no scratch-database apply on node-27 while the RAID link is degraded (no test database may be
created there); the apply path is proved by the fake-database suite and the CI real-database lane. node-27 real-DB receipt pending:
an apply of the tool against a disposable database once the link is repaired. A production `--apply` needs
the owner's explicit approval of the dry-run numbers and is not part of this change's evidence.
