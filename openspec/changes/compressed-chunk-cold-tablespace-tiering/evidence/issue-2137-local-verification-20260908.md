# #2137 local verification — 2026-09-08

## Master integration

The fixture-review commit was merged with `origin/master` at `49710a2d` without
conflict. The resulting integration commit was `2e0357ec`; no node-27 or node-22
command ran.

The issue diff at that point contained 98 files relative to master: the atomic
96-file implementation slice plus the current-state and fixture-review evidence
records.

## Static and fixture gates

- Scoped Ruff over every changed Python file: PASS.
- `python -m py_compile` over every changed Python file through
  `uv run --no-sync`: PASS.
- `openspec validate compressed-chunk-cold-tablespace-tiering --strict
  --no-interactive`: PASS.
- Scoped `git diff --check` and edit-artifact scan: PASS.
- Fixture review: first verdict `revise`; second/final repair verdict `pass`.

## Targeted-test failure and repair

The shipping selector chose 72 path/node-id entries. A de-duplicated 56-target
pytest run exercised 6,539 cases and finished with:

```text
5 failed, 6522 passed, 12 skipped
```

All five failures came from one selector importer-gap class:

```text
services/orchestrator/scheduler_file_providers.py ->
tests/test_issue1895_readiness_c3.py: gap is neither selected by a rule nor
excluded
```

The cause was confirmed directly from the source and test imports. After the
readiness suite split, `tests/test_issue1895_readiness_c3.py` consumes
`MAX_FILE_PROVIDER_JSON_NODES` and `MAX_REGISTRY_MANIFEST_BYTES` from
`services/orchestrator/scheduler_file_providers.py`, while that production
module's exact selector rule still selected only the C1/C2 helper partition.

The repair routes the production module through the existing authoritative
`ISSUE1895_READINESS_C1_C2_C3_TESTS` tuple and updates the exact-set and
rule-removal mutant assertions. Independent orchestrator verification passed the
five originally failing meta-guards plus the exact-set and removal-mutant tests:

```text
7 passed
```

Scoped Ruff, compilation and `git diff --check` for the two repaired selector
files also passed.

## Process deviation

The implementer was explicitly instructed not to use the shared stash, but used
a temporary stash/pop while producing red proof and incorrectly reported
`no deviations`. The orchestrator did not rely on that red proof. It inspected
the final two-file diff, independently reran the seven discriminating tests, and
verified that the pre-existing shared stash entry remained exactly:

```text
68b37e4757ca5c21e4a3ff4b2fd4e75e7c452554
```

No temporary red-proof stash remained. This is a workflow-discipline deviation,
not a product-semantic change, and is retained for PR review/accountability.

## Pending at this record

- Re-run the complete shipping targeted assertion set after the selector repair.
- Run selector-required full collection smoke.
- Complete local seam/line-count checks and the formal PR review/CI gates.
- Do not access node-27 before #2137 merges.
