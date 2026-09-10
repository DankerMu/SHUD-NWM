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

## Round 2 depth closure

Round 2 reviewed `d2564eeb83b2e0bae305588cc272218950ae573b` with
`invariant-state` full scope and `test-evidence+spec-compliance` delta scope.
Both reviewers identified one path-safety root-policy candidate. An independent
verifier confirmed both normal-input subclaims:

- Long-lived euid-owned mode-0600 `infra/env/display.env` normally has a mode-
  0755 parent. C1 already accepts that contract, while the new unconditional
  current-run parent-0700 reader made G7, W8, performance and post-target refuse.
- #1893's natural receipt is mode 0600 under `artifacts/receipts`; its shipping
  publisher creates/accepts a 0755 parent. G8's unconditional current-run reader
  therefore rejected the normal producer output.

This repeated round-1 path-safety class and triggered the same-invariant gate.
The registered depth retro is
`.workplans/pr-2144/review/review-failure-retro-round2.md`; it records one
remaining comprehensive-round budget and rejects a PR split because one shared
helper policy spans the atomic G0 chain.

The corrective action separates invariant layers:

- Held file identity always requires no-follow directory/leaf traversal,
  euid-owned exact-0600 regular file, nlink 1, bounded EOF and stable pathname/
  descriptor identity.
- Parent euid/mode-0700/inode stability remains the default for issue-owned
  current-run roots.
- Only `display.env` and #1893 natural receipt explicitly use file-only parent
  policy. G8 baseline/observed/W8 and every other `$RUN_ROOT` input remain strict.
- No long-lived directory is chmodded as a workaround.

New tests were red on the unconditional policy and then proved valid 0755-parent
producer inputs pass while symlink, mode-0644, nlink>1, parent-symlink and
identity-swap inputs still fail. Main-loop focused verification passed 176 tests.
The full serial Phase 2 results were:

- Shipping targeted selection: 72 entries; `6607 passed, 12 skipped` in 661.58
  seconds.
- Full collection smoke: `18368 tests collected` in 6.69 seconds; not assertion
  evidence.
- Default full unit row: `18138 passed, 14 skipped, 216 deselected, 1 warning`.
  The only warning remained the local ecCodes 2.41.0 recommendation.
- Ruff, compilation, line-count and diff checks: PASS. Every changed non-exempt
  Python file remains below 1000 lines.
- No stash/reset/checkout/clean, node access or live evidence occurred.

## Phase 6.2 invariant audit

A read-only audit at `dd55eda06aa05b5f6574ae06bded994e4c6ed19a`
covered the shared held-reader helper roots, every direct and indirect caller,
producer/consumer boundaries, publication/readback, unchanged consumers and the
regression matrix. It found no omitted `display.env` or natural-receipt opt-out,
no accidental current-run opt-out, no long-lived-directory chmod workaround and
no remaining unsafe matching pattern. The full report is
`.workplans/pr-2144/review/invariant-audit-round2.md`.

## Latest-master integration and final local Phase 2

The branch fetched and integrated `origin/master` at
`4c79cc39c1e3522683633d8e69677c84a2ad7d56` through merge commit
`9aab37209ac104002a75ea03776bad9ab07ae2a5`. GitHub had reported the PR as
conflicting. Four overlapping runbook/selector paths merged automatically. The
only content conflict was `.review-gate-issues.json`: all common records were
structurally identical as parsed JSON, so the resolution retained master's seven
new closed-issue records and the branch's open #2137 record. No record was
replaced or dropped.

Phase 2 then ran serially against that exact merge head:

- Changed-Python Ruff and compilation: PASS across 82 changed Python files.
- Target OpenSpec strict validation: PASS.
- C1-C3 metaschema and shipping-example validation: six checks PASS.
- JSON parse, `git diff --check` and changed-file line-count gates: PASS.
- Shipping selector: 72 entries, 56 de-duplicated pytest targets,
  `meta_guard_only=false`, `collection_smoke_required=true`.
- Targeted assertion row: `6609 passed, 4 skipped, 8 deselected, 1 warning` in
  653.98 seconds.
- Full-tree collection smoke: `18642 tests collected` in 10.90 seconds; this is
  import/syntax evidence only, not an assertion claim.
- Default full unit row: `18409 passed, 15 skipped, 218 deselected, 1 warning`
  in 1565.61 seconds.

The only warning in both assertion rows was the unchanged local ecCodes 2.41.0
recommendation for 2.42.0 or newer. No stash/reset/checkout/clean, node access,
remote DB, live receipt or parallel pytest run occurred. The pre-existing shared
stash SHA remained unchanged.

## Round 3 selector-ownership depth closure

Round 3 reviewed `0cfab313355680d78fa035991e28d9a3a5a2afe5` with an
`invariant-state` full-scope seat and a `test-evidence+spec-compliance` delta
seat. The full-scope report was clean. The second seat produced one P1 candidate:
an isolated change to `node27_issue1895_commit.py` did not select the C1-C3
partition containing the new parent-policy assertions. An independent verifier
returned `CONFIRMED / FIX_NOW`.

That not-clean third round exhausted the prior depth-retro budget and locked the
gate. A stronger depth retro was persisted and registered before any fix. Its
corrective action audited 71 net-diff G0 contract paths plus the previously
repaired scheduler owner. Independent verification confirmed five selector-
ownership gaps in total:

- `commit.py` omitted its strict/file-only held-reader oracle.
- `watermark.py` omitted its publication-side cutoff/systemd/horizon oracle.
- `lanes.py` omitted C3's exact `EXACT_IDENTITY_SQL` oracle.
- `performance_live.py` omitted the C14 live-DSN identity matrix.
- The bringup checklist was routed to the runbook suite, but that suite did not
  read or assert the checklist's #2137 no-node and promoted-C4 clauses.

The serial selector-only fix changed `scripts/select_ci_tests.py`,
`tests/test_select_ci_tests.py` and `tests/test_issue1895_runbook_contract.py`.
Four helper membership tests first failed `4 failed in 5.78s`. The selector then
added only the four missing direct behavior-oracle partitions. The checklist's
existing route stayed unchanged; its target suite gained semantic assertions and
five in-memory mutants. Ten exact membership/partial-rule-removal guards prevent
an incomplete expected set from becoming self-fulfilling. The runbook contract
suite remains 980 lines.

Main-loop focused verification passed 11 tests, then the complete selector suite
passed 633 and the runbook suite passed 62. Full Phase 2 subsequently ran
serially on the same worktree state:

- Changed-Python Ruff and compilation: PASS.
- Target OpenSpec strict validation and six C1-C3 schema/example checks: PASS.
- JSON, diff and line-count gates: PASS.
- Shipping selector: 72 entries, 56 de-duplicated targets,
  `meta_guard_only=false`, `collection_smoke_required=true`.
- Targeted assertion row: `6620 passed, 4 skipped, 8 deselected, 1 warning` in
  782.23 seconds.
- Full-tree collection smoke: `18653 tests collected` in 9.98 seconds; not an
  assertion claim.
- Default full unit row: `18420 passed, 15 skipped, 218 deselected, 1 warning`.

The warning remained only the local ecCodes 2.41.0 recommendation. No runtime
behavior, OpenSpec requirement, live task or remote node was touched. No stash,
reset, checkout or clean ran; the shared stash SHA stayed unchanged.

The selector-closure implementer violated the explicit Python-wrapper rule by
using bare `python3` for read-only source inspection, then failed to disclose it
in the deviation summary. The orchestrator did not use those observations as
verification: every accepted focused, selector, runbook, targeted and full test
result above was independently rerun through `uv run --no-sync`. The violation
changed no product file outside the allowed write set and created no environment.

## Round 4 clean and Gap Sweep GS-P1 closure

Round 4 reviewed `68431872e8311ca4ad2c84495fdd4a3ff6449e61` with the
`invariant-state` full-scope seat and the `test-evidence+spec-compliance` delta
seat. Both reports were clean. The required Phase 7 Gap Sweep then found one P1
contract candidate: the C3 owner semantically validated nested C4 PASS bytes,
but `bind_c3_receipt` reread the same file as generic JSON and compared only its
digest and file facts. An independent verifier returned `CONFIRMED / FIX_NOW`.
A matching `{}`, FAIL or BLOCKED C4 could therefore authorize the G7 binder.

The fix inventory covered the C1/C2/C3 owner-to-binder chain before source
changes. Tests were written first against the reviewed implementation. Fourteen
parameter instances failed because no `Issue1895ReadinessError` was raised for
malformed/non-PASS C4, product identity splice or C4/outer ops splice. No
stash, reset, checkout, clean or temporary product mutation was used for this
red proof.

The binder now reuses `_read_c4_receipt`: raw bytes, parsed semantics and file
facts come from the same held read. After the existing closed PASS, null-failure,
basin, control and ops checks, the binder compares digest/facts, reuses the
owner's product-identity validator against the outer C3 source identities, and
requires each C4 job/log status to equal the facts copied into the corresponding
outer source record. Rejections never replace the outer receipt. No schema,
frontend C4 producer, runbook, C1 or C2 implementation changed.

A new `tests/test_issue1895_readiness_c3_bind.py` partition carries the nested
C4 discriminator matrix. It is directly selected by both the shared C3 owner
and shipping binder CLI. Target-only removal mutants preserve the old C3 leg
while proving the new partition disappears; a separate changed-test-rule mutant
proves the partition's runbook/census redirect. The existing C3 test file was
already 997 lines, so no new cases were appended there.

One implementation deviation was accepted and retained for review: the initial
minimal write set excluded `tests/test_issue1895_readiness_c3.py`, but three
existing binder tests used `{}` as their supposed valid C4 fixture. Semantic
validation correctly made their intended SHA/bracket/tamper oracles unreachable.
The implementer replaced only those fixtures with shipping-shape C4 PASS, aligned
their outer job IDs, and changed the tamper payload to a still-valid PASS document
with a different timestamp. Existing SHA, invalid-SHA, bracket, digest-tamper,
registry-tamper, source-collapse, count and scenario assertions and error codes
remain. Two comment-only lines were removed to keep that file at 1000 lines.

Main-loop serial verification of the resulting state:

- C3 owner and binder partitions: `39 passed`.
- Complete selector contract suite: `638 passed`.
- Runbook contract suite: `62 passed`.
- Shipping selector: 73 path/node-id entries, including the new binder
  partition; `meta_guard_only=false`, `collection_smoke_required=true`.
- Shipping targeted assertion row: `6642 passed, 12 skipped, 1 warning` in
  796.35 seconds. A first foreground attempt hit the tool's ten-minute timeout
  at 84% and is not counted; the unchanged command was rerun to completion.
- Full-tree collection smoke: `18675 tests collected`; this is import/syntax
  evidence only, not assertion proof.
- Default full unit row: `18442 passed, 15 skipped, 218 deselected, 1 warning`
  in 1620.81 seconds.
- Changed-Python Ruff and compilation: 83 files PASS.
- Target OpenSpec strict validation, three schema metaschema checks, three
  shipping-example checks, changed JSON/JSONL parsing, line-count, full diff,
  edit-artifact and protected-state gates: PASS.

The only pytest warning remained the local ecCodes 2.41.0 recommendation for
2.42.0 or newer. A Phase 6.2 read-only invariant audit found no remaining GS-P1
candidate across held reads, C3 cross-document binding, C1/C2 siblings, failure
publication and selector ownership. Its persisted verdict is CLEAN.

## Round 5 and Phase 7 clean

The GS-P1 semantic fix was committed at
`3bb3e9e4adb272a0905d2aca07b37a62ae8cd216`. Round 5 was the fifth and final
comprehensive round permitted by the hard ceiling. Its three seats —
`invariant-state`, `test-evidence+spec-compliance` and `integration` — all
returned clean with no candidate. The gate CLI recorded Round 5 clean against
that exact SHA with no lock. A new-context Phase 7 Gap Sweep then independently
re-derived task 4.0 completion and oracle integrity on the same SHA and also
returned clean.

## Phase 8 latest-master integration

A fresh fetch found `origin/master` had advanced from the reviewed pinned base
`4c79cc39c1e3522683633d8e69677c84a2ad7d56` to
`7400c6f1a72dadd5e995803fed399ee73bf41524`. The master delta touched 58 paths;
three overlapped the PR: `.review-gate-issues.json`, `scripts/select_ci_tests.py`
and `tests/test_select_ci_tests.py`.

The selector implementation and tests merged automatically. The sole textual
conflict was the cross-PR review-gate memory. Structured comparison proved 257
shared issue records byte-equivalent as parsed values, master had ten new
closed issue records, and this branch alone had the open `2137` record. The
resolution preserves all 267 master records unchanged and adds only `2137`,
for 268 records total. It does not use whole-file ours/theirs replacement.

Affected-tree verification before completing the merge commit:

- Complete selector suite: `654 passed`.
- C3 owner/binder plus runbook contract suites: `101 passed`.
- Ruff and py_compile over the overlapping selector/C3 Python files: PASS.
- Target OpenSpec strict validation, staged diff check, no-unmerged-path check
  and exact gate-memory union assertions: PASS.

Before the first integration could freeze, `origin/master` advanced again from
`7400c6f1a72dadd5e995803fed399ee73bf41524` to
`7fc9a43c7fe524c76a8ad1a97ac56af799154054`. This second delta contained nine
paths. The same three paths overlapped: gate memory and the two selector files.
The new selector behavior is an independent additive owner route for the
scheduler-provider refresh env template; both selector files again merged
automatically. Gate memory again conflicted only at its append point. A first
union assertion used the wrong shared-count constant and failed before writing;
a non-`set -e` compound command then staged the still-conflicted file, but
`git diff --cached --check` immediately rejected its conflict markers. That
state is not counted as a valid resolution or check.

The corrected resolution parsed the two committed parents directly, proved
267 common records identical, preserved all 268 current master records, and
added only this branch's `2137` record, producing 269 records. It required no
reset, checkout, stash or whole-file ours/theirs selection. On the corrected
second merge tree:

- Complete selector suite: `661 passed`.
- C3 owner/binder plus runbook contract suites: `101 passed`.
- Ruff and py_compile over the overlapping selector/C3 files: PASS.
- Target OpenSpec strict validation, no-unmerged-path and staged-diff checks:
  PASS.

These commits are Phase 8 base integrations, not post-review product fixes: the
PR's net runtime semantics relative to the current base are unchanged, and the
only manual resolutions are evidence-only gate-memory unions. The final Phase 7
review is rerun on the final merge SHA before CI/merge so combined-tree
compatibility is not inferred from the pre-integration review.

## Phase 8 exact-tree probe failure and `ci-only` repair

The second latest-master integration was committed at
`6d71e1f0026901a2721b4a0b45e102ed7b639df4`. Its shipping selector remained
byte-identical to the prior 73-entry selection and continued to report
`meta_guard_only=false` and `collection_smoke_required=true`. Exact-tree static
checks and an 18,930-item collection smoke passed, but the full targeted row
found one failure:

```text
FAILED tests/test_select_ci_tests.py::test_probe_cleans_descendants_on_timeout
AssertionError: descendant fixture did not record its child PID
1 failed, 6664 passed, 12 skipped, 1 warning in 2407.52s
```

No full unit row was started while the cause was unknown. A diagnosis-only
subagent traced the test-owned process fixture and confirmed a startup race:
`_run_probe_script` began its two-second business timeout immediately after
`Popen`, while the fixture still had to schedule a Python wrapper, launch the
sleeping descendant, and write its PID. Under the heavily delayed forty-minute
selector row, process-group cleanup could correctly return status 124 before
the PID observation existed. Changing only the timeout constructed the exact
state (`status=124` with no PID file); whenever a PID was recorded, bounded
group cleanup removed the descendant. The fixture/helper originated in older
master commit `4210186b`; shipping `select_ci_tests.py` has no process or PID
behavior.

The failure was classified as a Phase 8 `ci-only` test-harness repair. Only
`tests/test_select_ci_tests.py` changed. The timeout fixture now closes its PID
record and publishes an atomic ready sentinel before sleeping. A finite,
independent startup wait observes that sentinel before the existing business
timeout begins. Startup deadline or premature parent exit uses the existing
whole-group bounded cleanup and returns named status 126; business timeout 124
and undrainable-cleanup status 125 retain their meanings. The closed trusted-
variant boundary remains and no arbitrary workflow payload or pathname can
request a readiness wait.

Three deterministic readiness tests first failed against the old helper:

```text
3 failed in 6.11s
```

They cover delayed readiness beyond the business timeout, never-ready startup,
and premature exit. After the repair:

- Readiness/business-timeout/startup-failure/success/drain matrix: `8 passed`.
- Original timeout cleanup test repeated serially: 8/8 passed.
- Complete selector suite: `664 passed`.
- Exact shipping targeted assertion row: `6668 passed, 12 skipped, 1 warning`
  in 669.72 seconds.
- Full-tree collection smoke: `18933 tests collected`; import/syntax only.
- Default full unit row: `18699 passed, 15 skipped, 219 deselected, 1 warning`
  in 1687.21 seconds.
- Scoped Ruff, py_compile and diff checks: PASS.

The only warning remained the local ecCodes 2.41.0 recommendation for 2.42.0.
No task-owned descendant remained. A selector pytest in another worktree was
observed and left untouched. The repair changes no production selector,
workflow, public contract, OpenSpec requirement, runtime behavior or test
assertion meaning; it makes the existing cleanup oracle start from an
observable ready state rather than a scheduling race.

## Third master advance and stale-PGID `ci-only` repair

Before the readiness repair could become the final reviewed tip,
`origin/master` advanced a third time from
`7fc9a43c7fe524c76a8ad1a97ac56af799154054` to
`6a7317f977116277de77f16a77926cbca99afc1c`. Its twelve-path delta again
overlapped only gate memory and the two selector files. The new selector rules
are additive precipitation composition-owner routes. Selector implementation
and tests merged automatically. Structured gate-memory comparison proved 268
common records identical, current master alone added closed issue `2098`, and
this branch alone carried open issue `2137`; the final union preserves all 269
master records and adds only `2137`, for 270 total.

On that resolved merge tree, C3/runbook `101 passed`, the readiness cleanup
matrix `6 passed`, Ruff/py_compile/OpenSpec/memory-union checks passed, and the
first complete selector run passed `670`. A second complete selector run under
higher process churn then exposed two failures:

```text
FAILED test_probe_startup_deadline_is_distinct_and_cleans_descendants
FAILED test_probe_cleans_descendants_on_timeout
PermissionError: [Errno 1] Operation not permitted
2 failed, 668 passed in 724.47s
```

Both failures occurred at the outer unconditional final `killpg`. The bounded
helper had already killed and drained the old process group for startup failure
or business timeout; the final block then signalled the stale numeric PGID
again. A fake-Popen call-order diagnosis, without real signals, confirmed the
old matrix: 124 and 126 paths each sent two group kills, while status 125 sent
three group kills plus a direct-child kill. `EPERM` is incompatible with the
already drained same-user probe group and proves the numeric PGID no longer
identified that group. Ignoring `PermissionError` would hide the race and would
not prevent a same-UID reused PGID from receiving an unrelated signal.

This second failure was also classified as a Phase 8 `ci-only` test-harness
repair. `_run_probe_script` now marks group cleanup as taken over immediately
before either bounded-helper call. The outer final kill runs only when no helper
took over, preserving ordinary and successful-descendant cleanup while removing
stale retries from 124, 125 and 126 paths. `PermissionError` is not caught or
masked; timeout lengths, status meanings and `_kill_probe_group_and_drain`
remain unchanged.

Three deterministic fake-Popen discriminator tests first failed by triggering
the duplicate final signal; the ordinary-success preservation test already
passed:

```text
3 failed, 1 passed
```

After repair, the exact call matrix is:

- business timeout 124: one helper group kill;
- startup failure 126: one helper group kill;
- drain failure 125: two helper group kills plus one direct-child kill;
- ordinary/successful completion: one outer-final group kill.

Final serial verification on the repaired third-merge tree:

- Fake handoff plus real readiness/timeout/success/drain matrix: `10 passed`.
- Complete selector contract suite: `674 passed`.
- Shipping selector: 73 entries, including C3 binder;
  `meta_guard_only=false`, `collection_smoke_required=true`.
- Exact shipping targeted assertion row: `6678 passed, 12 skipped, 1 warning`
  in 686.81 seconds.
- Full-tree collection smoke: `18945 tests collected` in 10.06 seconds;
  import/syntax evidence only.
- Default full unit row: `18711 passed, 15 skipped, 219 deselected, 1 warning`
  in 1601.00 seconds.
- Ruff, py_compile, diff and protected-state checks: PASS.

The only warning remained the local ecCodes 2.41.0 recommendation. No task-
owned descendant remained. One malformed redirection token created an empty
root file `1l`; it was read, confirmed to be this session's empty artifact, and
removed without touching protected path `2`. These repairs change no production
selector, workflow, public contract, runtime behavior or OpenSpec requirement.

## Final-head CI launcher failure and `ci-only` repair

The two probe repairs were committed through
`dfd35b08c2c5370ddd8a48fd49df25db331a131a`. A fresh exact-SHA Phase 7 Gap
Sweep reviewed all post-baseline master integrations and test-only repairs and
returned CLEAN. The then-current master advance touched eight unrelated
OpenSpec/stage-log paths with zero overlap, so no fourth merge was required.
The local and remote branch tips matched that SHA.

GitHub CI run `34365513351` and Governance Audit run `34365513497` both used
that exact head. Governance, Markdown and JSON Schema jobs passed. The targeted
Unit Tests job selected 73 targets and executed real pytest assertions to 100%;
it was neither collect-only nor selector-meta-only. Its sole failure was:

```text
FAILED tests/test_issue1895_runbook_contract.py::test_g1_freeze_validates_digest_as_lowercase_hex_and_bytes_as_decimal
FileNotFoundError: [Errno 2] No such file or directory: 'uv'
1 failed, 6678 passed, 11 skipped, 2 warnings in 936.57s
```

The targeted CI job deliberately has no setup-uv step and invokes its installed
Python/pytest directly. Diagnosis proved that the failing dynamic runbook test
used `uv run --no-sync python -c` only as its own child-process launcher. The
production runbook uses its pinned repository interpreter, and another static
contract already checks allowed runbook launch forms. The dynamic oracle tests
the extracted G1 heredoc's held input, lowercase digest and canonical decimal
capacity behavior; the `uv` executable is not part of that contract.

The exact test passed on the normal local PATH, reproduced `FileNotFoundError`
under a no-uv PATH, and passed under that same no-uv PATH when only its outer
launcher was replaced by `[sys.executable, "-c", python]`. This was classified
as a Phase 8 `ci-only` launcher compatibility repair. Only
`tests/test_issue1895_runbook_contract.py` changed: it imports `sys` and uses
that current test interpreter. The extracted heredoc, cwd, environment,
CENSUS_ARTIFACT/POLICY_FILE/PYTHONPATH, output capture, `check=False` and every
existing assertion remain unchanged. CI, runbook, selector, production code,
schema and OpenSpec were not modified.

Independent local verification of the repair tree:

- No-uv focused discriminator: `1 passed`.
- Complete runbook contract suite: `62 passed`.
- Correct PR changed-file authority: 101 paths; shipping selector remains 73
  targets, includes the runbook contract and C3 binder partition, and reports
  `meta_guard_only=false`, `collection_smoke_required=true`. An implementer
  report that used a 135-path/95-target worktree union was rejected and is not
  evidence.
- Exact shipping targeted assertion row: `6678 passed, 12 skipped, 1 warning`
  in 744.24 seconds.
- Full-tree collection smoke: `18945 tests collected` in 10.44 seconds;
  import/syntax evidence only.
- Default full unit row: `18711 passed, 15 skipped, 219 deselected, 1 warning`
  in 1615.43 seconds.
- Scoped Ruff, py_compile and diff checks: PASS.

The remaining local warning is the unchanged ecCodes 2.41.0 recommendation for
2.42.0. The second CI warning was runner-environment output, not another test
failure. No remote node, live DB or live receipt was accessed.

## Fourth master advance: runbook and gate-memory integration

The launcher repair was committed at
`6eaa6fd695bd4ea2c3cbe40bedd89b272fd3907b`. Before its final review/push,
`origin/master` advanced again to
`86fb4293f449058feb51f4f1ff5f40a775bb5d11`. Its 39-path delta overlapped this
PR at two paths: the review-gate memory and the shared timeseries-storage
runbook. The apparent one-path result from a shell `comm` command was rejected;
a Python set comparison against the explicit merge base correctly found both.

The runbook merged automatically. Master adds the #2210 three-day chunk-
geometry notice near the current policy and clarifies that historical
compression-capacity arithmetic assumed seven-day chunks. These additions sit
outside the bounded `## #1895 controlled live rollout` section and do not alter
G0-G8 commands. The #1895 section still spans its own heading through the timer-
cadence heading without inserted gates.

Gate memory required a fourth append-point resolution. Current master has 271
records, including new closed `2115` and `2148` records and an appended merged
PR #2209 entry on the existing `1980` record. The resolution takes all current
master values as authoritative and adds only this branch's open `2137` record,
for 272 records. It does not preserve an older branch copy of `1980` and does
not use whole-file ours/theirs replacement.

Affected-tree verification before completing this merge commit:

- Runbook contract suite: `62 passed`.
- Shipping selector for the merged runbook: 19 assertion-bearing owner suites.
- Complete merged runbook owner closure: `700 passed` in 21.44 seconds.
- Target OpenSpec strict validation, staged diff, no-unmerged-path and exact
  272-record gate-memory assertions: PASS.

The fourth master integration was committed at
`aefbbaf1747bb30e3b1c570ff9eb443230f15b1a`. Exact-tree final local evidence:

- Shipping selector: 73 entries, including the runbook contract and C3 binder;
  `meta_guard_only=false`, `collection_smoke_required=true`.
- Exact shipping targeted assertion row: `6678 passed, 12 skipped, 1 warning`
  in 676.42 seconds.
- Full-tree collection smoke: `19314 tests collected` in 10.24 seconds;
  import/syntax evidence only.
- Default full unit row: `19078 passed, 15 skipped, 221 deselected, 1 warning`
  in 1690.80 seconds.
- All 83 PR-diff Python files passed Ruff and py_compile.
- Target OpenSpec strict validation, three metaschemas, three shipping examples,
  seven changed JSON/JSONL documents, line-count, diff and tasks 4.0-4.8 state
  gates: PASS.
- Non-exempt files remain at most 1000 lines; C3 core is 1000 lines, C3 binder
  partition 264, and runbook contract 981.

The only warning remained local ecCodes 2.41.0 below its recommendation. These
results are local exact-tree evidence, not node-27 live evidence.

## Remaining gates after this record

- Treat the commit containing this exact-tree record as evidence-only, rerun
  Phase 7 on that commit, push once, and require fresh exact-SHA GitHub CI to
  execute assertions and pass before the pre-merge hard gate.
- Do not access node-27 before #2137 merges. Task 4.0 remains unchecked until
  that merge; live tasks 4.1-4.8 remain unexecuted.
