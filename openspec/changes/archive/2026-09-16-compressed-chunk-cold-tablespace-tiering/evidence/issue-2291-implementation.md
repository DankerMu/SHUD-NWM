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
Those initial results apply only to their stated SHA; corrected results follow.

## Corrected implementation verification

At `e36f65e5d5229c19f3c789cdc6a8e4308c829f7c` on the same owned node27
checkout/environment, run serially:

- Focused public count, storage/publication/C14, census, executable runbook and
  selector suites:1147 passed (223.98s).
- Pinned runtime engine:1 passed,1 deselected (10.03s).
- Diff-selected regression:4665 passed,5 skipped (757.64s).

SHA-bound driver: `.workplans/issue-2291/run-selected-node27.sh`; terminal
statuses: `.workplans/issue-2291/selected-driver-node27.log`. The focused set
includes the same full N=1/3/63 frozen-file consumer chains, all five documented
CLI launches with both G8 occurrences, and actual extracted G8 derivation
commands refusing symlinked observed-file inputs. Obsolete script-spelling
assertions were replaced with execution, not re-pinned to new wording.

Actual module CLI smoke at `6e944e4b13b40d66755a57b38671d9a35ce18908` also
reached `CENSUS_JSON_INVALID` for a missing original: exit1, no stdout, no DSN.
No import traceback remained. The temporary tests-first driver is removed from
the final tree after this proof; its source remains at the tests-only commit.

Default full regression, independent code cross-review/final review and CI are
still separate premerge requirements. No preparation result authorizes
production G0-G8 or closes #1895/#1891. #2291 merge requires a new human decision.

## Round1 wrapper finding and tests-first repair

Four independent seats reviewed integrated
`2f96addbac5f95ec0bb4a9b73bc3b82d4aa47c26`. Correctness's initial attempt exited
without a verdict and was recovered as the same seat/round; no counter reset.
Only CAND-SP-01 remained: wrapper/P1, independently CONFIRMED/FIX_NOW by
CountVerifyWrapper. G3's second observation opener/session could emit a raw
driver/OS traceback after successful watermark. Real credential leakage was
not proven; the deterministic regression injects a distinctive fake DSN marker.

At tests-only `9491cad36ffa38cbf621fa10df2b4f9e098c15b8`, node27 executed:

```sh
uv run --no-sync pytest -q tests/test_issue2291_reviewed_census_count.py \
  -k cutoff_count_closes_post_watermark_driver_failures
```

All4 OS/driver × open/session cases failed semantically (96 deselected,0.52s),
after the first watermark connection succeeded and closed. Raw exceptions escaped
instead of stable refusal; no missing import/signature failure was counted.
`wrapper-red-node27.log` and `run-wrapper-red-node27.sh` bind the evidence.

The repair adds the existing census-style residual public exception boundary
only to new G3 main; typed refusal codes and finally cleanup remain. G8 and shared
connection owners are untouched. Stale six-group G5 help is removed as a cleanup
rider. Corrected-head green and round2/final/full/CI evidence remain separate.

The pre-integration full baseline at
`a18def5da632068370f1af2303182ba758330829` completed19837 passed,330 skipped,
1 optional missing-ecCodes warning (3591.90s). It is not evidence for the
integrated repaired head; that head requires its own full regression.
