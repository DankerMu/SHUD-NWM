# #2123 selector assertion CI evidence

PR #2126 run `34174843099`, job `101902127375` (“Unit Tests”), head `775dfe7c9fe6652826fd2bb9e47ae24084a7a76f`.

The changed-file selector chose exactly `tests/test_select_ci_tests.py`; CI then ran that test file’s assertions and reported:

```text
593 passed in 150.63s (0:02:30)
Targeted test files:
  tests/test_select_ci_tests.py
Selection collapsed to the selector meta-guard — also running collect-only smoke
```

The full-tree collect-only ran **after** the 593 asserted tests as an additional import/syntax guard. This was not a zero-assertion fallback. Therefore `test_c4_schema_only_change_runs_frontend_ajv_negative_suite` executed in CI together with the selector suite.

Later fix commits through `7a4ada4c` changed C4 binder/lane/RBAC/store tests, one store normalization line and C4 spec wording; they did not change `.github/workflows/ci.yml` or `tests/test_select_ci_tests.py`. Final pushed-tip CI must still pass before merge; this earlier run closes task 3.6’s requirement that the Python selector hunk actually execute rather than relying only on static inspection.

No local backend pytest/collect claim is made. Job log was read once from GitHub; no node-27/node-22 access or live execution.
