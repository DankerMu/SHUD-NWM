# #2291 implementation evidence

## Tests-first checkpoint

Tests-only checkpoint: `9d6d7fa832f17067f842efd1575eb48f3c3273e0`.
Production sources remained unchanged from the approved fixture checkpoint.
Parent executed on owned node27 checkout, isolated test-venv and TMPDIR with
an empty inherited environment (no production DSN):

```sh
PYTHONPATH=. uv run --no-sync python .workplans/issue-2291/red-count-proof.py
```

The real census owner accepted the complete simulated three-group population
and emitted GO, required/resolved3, eight members and two parity rows per group.
The existing downstream public helpers then produced eight semantic failures:

- G5: valid N3 refused with `CENSUS_KEYS_DRIFT`.
- G6 calls1,2,3: each refused with `RECEIPT_KEYS_INVALID`.
- G8 named observation: `POST_TARGET_BASELINE_COUNT`.
- G8 baseline persistence and exact reconciliation: `BASELINE_COUNT_INVALID`.
- Census CLI parser accepted unsupported reviewed64.

The driver exited1 as expected; wrong exceptions/import/signature errors were
not counted as red. Raw output and SHA-bound driver are retained in
`.workplans/issue-2291/red-node27.log` and `run-red-node27.sh`.
This is executed regression evidence using simulated catalog boundaries, not
pinned-engine proof or a production observation.

## Tests-only checkpoint boundary

The tests-only checkpoint made no green, pinned-engine, default-regression,
selector, review or CI claim. Subsequent results below identify their own SHA.
No production DB/active-checkout or G0-G8 operation was performed.
#1895/#1891 and shared production tasks remain open.

## Initial implementation validation

At `230e8c5817d038b933c32f8210e4c1a05d594d39`, local Ruff and strict OpenSpec
passed. Node27 focused run:1131 passed,4 failed. Failures identified an unmigrated
partial G1 policy test and the missing new-suite importer leg in the existing
shared-fakes selector rule. Repairs preserve original closure/removal guards.

The pinned PG15.2/TimescaleDB2.10.2 runtime oracle at that same SHA passed:
1 passed,1 deselected. It exercised real admitted narrow/forcing count4,
compressed excluded legacy/third tables, differing1/7-day ranges, added admitted
origin count5 and census NO-GO, then restored fixture state. Existing physical
parent/origin/role/plan/recompression/rollback/cleanup assertions remained invoked.

Actual clean-env CLI process smoke also caught direct-file launch failing with
`ModuleNotFoundError: scripts` before validation. The runbook now uses the
repository's existing `python -m scripts.<module>` convention for all five
affected original-loading entrypoints. Added subprocess coverage extracts those
actual launches and reaches original refusal without PYTHONPATH, database
observation or publication. This documented invocation change is a recorded
plan deviation; no sys.path shim, package change or fallback was introduced.
Corrected-head green remains to be executed; earlier results are not its proof.
