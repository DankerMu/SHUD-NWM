# Tasks: retry path divergences are intended (#3319)

Fixture level: `minimal`. Risk packs: none (no behaviour change).

## Must preserve

- Every assertion of `tests/test_retry_accounting_table.py` and every computed value of
  `services/orchestrator/retry_identity.py`. A diff of this change that alters an expression, a
  constant, a parametrised value or an assertion is out of scope.

## 1. Fixture

- [x] 1.1 Fixture review (one read-only reviewer); `openspec validate retry-path-divergences-are-intended --strict --no-interactive` passes.

## 2. Code comments (implementer)

- [x] 2.1 `services/orchestrator/retry_identity.py`: the docstring of `next_after_failure_non_master` and the comments
      of `MANUAL_RETRYABLE_STATUSES` / `UPSTREAM_REFRESH_RETRYABLE_STATUSES` state the decision (kept on purpose,
      #3319, 2026-11-26) and the reason in one or two sentences each.
- [x] 2.2 The other places that say "#3319 owns any behaviour change" (`stage_job_sort_attempt`,
      `next_manual_attempt`) say that the difference is pinned and that a change needs its own behaviour-change issue;
      they do not claim a decision that was not made.
- [x] 2.3 `tests/test_retry_accounting_table.py`: the one comment that names #3319 says that `retry.py`'s sets stay
      separate from the owner's sets. #3319 decided what the upstream-refresh set contains, not whether those sets
      merge: the comment is reworded to say that the separation is pinned and that merging needs its own issue,
      without claiming a decision. No assertion, parameter or test name changes.
- [x] 2.4 A table in the report maps every scenario of the two new requirements to the test id that asserts it. A
      scenario with no asserting row gets one (plain-data row in the existing parametrisation), and the report says
      which.

## 3. Evidence Floor

- [x] 3.1 `uv run --no-sync pytest -q tests/test_retry_accounting_table.py` passes; the collected count is unchanged unless 2.4 added rows (then the difference equals the added rows).
- [x] 3.2 The diff of `services/orchestrator/retry_identity.py` and of `tests/test_retry_accounting_table.py` touches
      only comment and docstring lines: for each file the AST of the base version and of the new version, docstrings
      removed, are equal (for the test file: equal except for the plain-data rows 2.4 adds, each listed).
- [x] 3.3 `uv run --no-sync ruff check --no-cache .` clean; entropy hard gate unchanged (247 findings / 0 failing).
- [x] 3.4 `openspec validate --strict` before the archive; the main spec validates after it.
- [x] 3.5 CI green on the PR.
