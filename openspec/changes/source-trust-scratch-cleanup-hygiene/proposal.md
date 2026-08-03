# Failure-path cleanup for the remaining two bare /scratch evidence writers (#1209)

## Why

The #1128 defect family — node-22 host-contract tests that write REAL
evidence under `/scratch/frd_muziyao` with cleanup sequenced AFTER
the assertions — has two remaining instances outside PR #1210's
claimed surface:

1. `tests/test_two_node_docker_source_trust.py:76-104`
   (`test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`):
   7 assertions at `:89-97` precede the manual unlink/rmdir cleanup
   at `:98-104`; no try/finally anywhere in the file (`grep -c
   "finally:"` = 0). Any assertion failure (a genuine contract
   regression, or host owner/permission drift making
   `returncode != 0`) skips the whole cleanup block, leaving a
   role-scoped report pair (`two-node-docker-source-trust-compute
   .json`/`.txt`) plus two directory levels on the SHARED node —
   debris that looks exactly like legitimate evidence
   (`evidence_run_id: source-trust-explicit` under the real
   evidence root). Additionally the bare `rmdir` at `:103` is a
   green-path backfire: any unexpected entry (a SIGKILL-orphaned
   `.<name>.<uuid>.tmp` sidecar from `atomic_write_bytes_no_follow`,
   or a concurrent run's file) makes the NEXT otherwise-green run
   red with an unrelated `OSError: Directory not empty`.
2. `tests/test_two_node_docker_runtime.py:3763-3777`
   (`test_static_report_explicit_evidence_run_id_overrides_scratch_path_inference`):
   `written_path.unlink()` at `:3777` sits after the assertions at
   `:3775-3776` — the sibling #1128 recorded as observe-only and
   #1209 explicitly adopts (end-state verified at master 8553f9ca:
   still bare).

These tests really run on node-22 AND GitHub CI (ci.yml provisions
`/scratch/frd_muziyao` in both jobs); only macOS skips them. The
defect changes no verdict — it is pure hygiene — but the debris is
indistinguishable from real preflight output when auditing node-22
evidence by hand.

## What Changes

The issue's recommended route, matching the #1128 fix shape (the
merged Class C form at `tests/test_two_node_docker_runtime.py:4431-4447`:
`try:` around action + assertions, cleanup in `finally:`):

1. **source_trust `:78-104`**: wrap the `_run_preflight` call and
   all 7 assertions in `try:`; move cleanup into `finally:` as
   best-effort `shutil.rmtree(evidence_root.parent,
   ignore_errors=True)` (the issue-sanctioned form). This preserves
   cleanup semantics on green (both files + both directory levels
   removed — rmtree strictly covers the old unlink×2 + rmdir×2) and
   additionally clears the two recorded backfire shapes (orphaned
   sidecar / concurrent debris) instead of redding on them.
   `ignore_errors=True` guarantees the finally can never mask the
   original `AssertionError` (the acceptance's no-swallow clause;
   the merged #1128 shape's bare `rmtree` would raise
   `FileNotFoundError` if the action failed before creating the
   tree — this change deliberately hardens that corner rather than
   copying it). Add `import shutil` to the file's stdlib imports.
   Assertion set, `evidence_run_id` semantics, scratch layout
   intent, and the `:72-75` skipif stay byte-identical.
2. **runtime `:3763-3777`**: pre-declare `written_path` as None,
   wrap the `write_static_report` call + assertions in `try:`, and
   move the file removal into `finally:` as a suppressed
   `(written_path or report_path).unlink(missing_ok=True)` inside
   `contextlib.suppress(OSError)` (cleanup semantics unchanged:
   file only, no directory removal — exactly what the bare `:3777`
   did; `missing_ok` covers the action-failed-before-writing case,
   the suppress covers `IsADirectoryError`/`PermissionError` when
   the fixed path is occupied by an unexpected entry — a raising
   finally would mask the original failure; preferring the bound
   `written_path` keeps the resolved-output semantics of the old
   code). Add `import contextlib` there.

Out of scope: `scripts/validate_two_node_docker_source_trust.py`
and `scripts/validate_two_node_docker_runtime.py` production logic
(zero diff); the `:72-75`/`:3759-3762` skipif guards; the fixed-path
concurrency collision and the never-reclaimed
`/scratch/frd_muziyao/nwm-test/` top level (recorded observations,
#1128); the module-scoped shared `scratch_evidence_root` fixture
(issue's alternative — deferred); hermetic conversion (rejected in
#1127 — the scratch path assembly would lose all coverage).

## Impact

- Affected code: `tests/test_two_node_docker_source_trust.py`
  (one test wrapped, `import shutil` added),
  `tests/test_two_node_docker_runtime.py` (one test wrapped —
  disjoint from the #1211 meta-guard and the #1128 Class C test).
  Final surface checked against `git diff master...HEAD
  --name-only` at evidence time.
- Affected specs: `two-node-docker-runtime` (1 ADDED requirement,
  scoped to the two named tests with an explicit Class C carve-out:
  they clean up on failure paths via non-raising forms).
- Frozen surfaces (zero diff): both validate_* scripts,
  `.github/workflows/ci.yml`, every other test in both files.
