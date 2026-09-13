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

## Remaining verification

Source implementation is in progress. No green result, isolated pinned-engine
result, default regression, selector closure, review result or exact-head CI is
claimed by this checkpoint. No production DB/active-checkout or G0-G8 operation
was performed. #1895/#1891 and shared production tasks remain open.
