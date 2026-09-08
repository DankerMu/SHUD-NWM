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

## Post-repair local results

- Shipping selector: 72 path/node-id entries; `meta_guard_only=false` and
  `collection_smoke_required=true`.
- Complete shipping targeted assertion run: `6527 passed, 12 skipped` in
  936.01 seconds. The only warning reported local ecCodes 2.41.0 below its
  recommended 2.42.0; no test failure occurred.
- Selector-required full-tree import/syntax smoke: `18288 tests collected` in
  11.49 seconds. This is recorded only as collection smoke, not assertion proof.
- C1-C3 schema negative/positive and runbook contract focus: `63 passed`.
- Shipping `check-jsonschema` metaschema plus example validation for all three
  changed C1-C3 schema/example pairs: six checks passed.
- Changed-file line-count gate: every new readiness Python file is at most 1000 lines
  except the pre-existing selector governance files
  `scripts/select_ci_tests.py` and `tests/test_select_ci_tests.py`.
- Full branch `git diff --check`: PASS.

## Full local regression

The default full unit row ran as:

```text
uv run --no-sync pytest tests/ -q --tb=short --durations=25 \
  -m "not e2e and not grib and not integration"
```

Result:

```text
18058 passed, 14 skipped, 216 deselected, 1 warning in 2334.56s
```

The single warning was the same local ecCodes 2.41.0 recommendation. The slowest
reported readiness case was the shipping C4-builder/C3 acceptance at 12.97
seconds; no test failed.

## Round 1 verified finding closure

PR #2144 round 1 reviewed `b04f4001e1f26d2abb50c560a27c42561edd389f`
with four high-risk seats. Eight raw P1 candidates deduplicated to seven. Four
independent verifier batches returned all seven as `CONFIRMED / FIX_NOW`:

- C1-C3 wrong/invalid SHA and out-of-bracket failure proof.
- C4 FAIL/BLOCKED rejection before C3 publication.
- C1-C3 example-only selector routing to the assertion-bearing C3 partition.
- Exclusive no-clobber DSN publication plus held no-follow `display.env` reads.
- Held private current-run reads across G1/G3/G4/G5/G6/G7/G8 authorizers.
- G1 SHA256 digest validation separate from canonical decimal capacity fields.
- G4 positive reserve/per-tick and bounded numeric UID/GID validation.

The first implementation pass left G6 group enumeration and G8 expected-value
derivation on pathname reads. The orchestrator rejected the claim that a later
safe binder could close a pre-mutation input; the same implementer added held
reads before preview, group selection and parameter derivation. Subsequent
surface scans found and closed sibling pathname reads in W8 DSN provenance,
performance bracket, filesystem receipt, systemd facts, valid-times baseline,
isolated oracle, runtime projection and installer receipts.

A final audit found two more issues before Phase 2:

- DSN `mode=0644` was published before readback rejection. Exact `0600` is now
  required before payload construction or publication, and invalid modes leave no
  file.
- G3 read held report bytes but took bracket mtime from a later pathname stat.
  Held JSON facts now carry `st_mtime_ns`, and the bracket uses metadata from the
  same descriptor as the content.

The reusable private reader uses the existing C1-C3 `_read_held_descriptor` and
therefore checks parent euid/mode-0700/inode before and after the bounded read,
file mode-0600/nlink-1/no-follow identity, exact EOF and pathname/descriptor
stability. It adds bounded JSON complexity without globally tightening C2 nested
evidence readers. Intentional known-file env replacement, producer-local root
evidence assembly, standalone non-G `publication_prove.py`, and the historical
runbook section outside G0-G8 remain explicitly out of this invariant.

Tests were added before production changes and observed red for each behavior.
The implementer reported the initial G6 scope judgment, one late G4 sibling,
minor test-fixture/order mistakes and the final G3 content/mtime split; none was
hidden as `no deviations`.

## Post-fix Phase 2

- Complete round-1 focused suites, including the full selector suite: `824 passed`.
- Shipping selector remained 72 entries and selected every modified acceptance
  partition. Assertion run: `6594 passed, 12 skipped` in 894.44 seconds.
- Selector-required full-tree import/syntax smoke: `18355 tests collected` in
  8.01 seconds; this remains collection evidence only.
- Default full unit row: `18125 passed, 14 skipped, 216 deselected, 1 warning`
  in 2001.31 seconds. The only warning remained local ecCodes 2.41.0 below the
  recommended 2.42.0.
- Changed Python Ruff, compilation, line-count gate and full diff check: PASS.
  `tests/test_issue1895_runbook_contract.py` was behavior-preservingly reduced
  from 1019 to 982 lines; only the two pre-existing selector governance files
  remain over 1000 lines.
- No stash/reset/checkout/clean, remote-node access or live receipt occurred in
  the round-1 fix pass. The pre-existing shared stash SHA remained unchanged.

## Pending at this record

- Complete the post-fix comprehensive review, Gap Sweep and GitHub CI gates.
- Do not access node-27 before #2137 merges.
