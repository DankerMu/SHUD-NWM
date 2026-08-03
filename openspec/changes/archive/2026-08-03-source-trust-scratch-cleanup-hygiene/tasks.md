# Tasks — source-trust-scratch-cleanup-hygiene (#1209)

Anchors verified at master 8553f9ca (this session, by direct read):
`tests/test_two_node_docker_source_trust.py:72-75` skipif (frozen),
`:76` target test def, `:78` evidence_root literal, `:80-87`
`_run_preflight` real write, `:89-97` the 7 assertions, `:98-104`
trailing manual cleanup (unlink×2 + rmdir×2), file has ZERO
`finally:` and imports no `shutil` (stdlib block `:3-10`);
`tests/test_two_node_docker_runtime.py:3759-3762` skipif (frozen),
`:3763` sibling test def, `:3765` `report_path` literal, `:3768-3773`
`write_static_report` call, `:3775-3776` assertions, `:3777` bare
`written_path.unlink()` — end-state check REQUIRED by the issue:
confirmed still bare at 8553f9ca (NOT covered by #1128/PR #1210);
the merged #1128 fix shape to match:
`tests/test_two_node_docker_runtime.py:4431-4447` (`try:` around
action+assertions, `finally: shutil.rmtree(evidence_root.parent)`);
`shutil` already imported in the runtime test file.

Risk triage: fixture level **compact** (S-size; two tests wrapped,
~12 lines net, zero production code). Risk pack selected:
**oracle-discrimination** (the finally must actually run on the
failure path AND must never swallow the original AssertionError;
green-path cleanup must stay complete). Not selected:
concurrency-lifecycle (no threads), record-forensic,
performance/UI/migration (n/a).

ORACLE ROUTING (recorded deviation from the issue's verification
wording, per the standing directive that node-22 is not used in this
run): the issue's acceptance items phrased as "on node-22" are
satisfied by the two accepted substitutes — (a) GitHub CI
ubuntu-latest, which provisions `/scratch/frd_muziyao` in both jobs
(ci.yml) and REALLY RUNS both touched tests (the accepted Linux
oracle recorded in this run's prior fixtures), covers the
success-path items: tests green, un-skipped, real scratch writes and
cleanup on a real Linux host; (b) the failure-path item (temporarily
break an assertion → cleanup still happens, original AssertionError
preserved) cannot run on CI without pushing a mutation commit and
cannot run on macOS (the skipif condition is TRUE there — a
writable `/scratch/frd_muziyao` cannot exist under SIP), so it is
proven by a SCRATCHPAD REPLICA with three hard constraints
(fixture-review P1-2/P2-4 rulings, path feasibility MEASURED):
(i) the replica MUST use production-whitelisted roots, because a
bare session-tmp root is REJECTED before any write — source_trust's
`_run_preflight` blocks it at
`scripts/validate_two_node_docker_source_trust.py:290-298`
(`rc=2`, nothing written) and runtime's `write_static_report`
raises `ValueError` via `ensure_approved_evidence_root`
(`scripts/validate_two_node_docker_runtime.py:1058-1074`) — use
`checkout / "artifacts" / "source-trust-explicit" /
"docker-security"` for source_trust (measured `rc=0`, both report
files written) and `report_path = tmp_path / "artifacts" /
"run-static-explicit" / "docker-security" / "static.json"` with
`repo_root=tmp_path` for runtime (measured: write succeeds);
(ii) the forced-false assertion MUST be a POST-write one (e.g.
mutate `summary["roles"] == ["compute"]`), never
`result.returncode == 0`, and the replica MUST first assert the
report file EXISTS before the forced failure (anti-vacuity
precondition — otherwise a blocked run reds with nothing written
and the pairing proves nothing);
(iii) the replica MUST be BOUND to the shipped body — build it via
`inspect.getsource` of the delivered test function with text
substitution of the root literal (and the forced assertion), then
exec/run it, so a mis-indented shipped `finally` cannot pass on a
hand-copied twin. Pair each fixed-body replica with the same
replica carrying the PRE-fix trailing-cleanup shape: fixed → red
with the ORIGINAL AssertionError AND tree gone; pre-fix → red AND
the report files SURVIVE. Replicas live in the scratchpad, never in
the repo. This routing is disclosed in the PR body.

Must-preserve behavior:

- Assertion sets of both tests byte-identical (only indentation
  changes from the try-wrap); `evidence_run_id` semantics, scratch
  path literals, and both skipif guards untouched.
- Green-path cleanup completeness unchanged or stronger:
  source_trust removes both report files and both directory levels
  (rmtree covers the old unlink×2+rmdir×2); runtime removes exactly
  the report file (no directory removal added — the old code never
  removed directories there).
- macOS: both tests still SKIP with the same reason strings; suite
  counts unchanged from baseline (measured on this machine at
  8553f9ca, combined two-file run: `436 passed, 3 skipped` —
  runtime file alone is `425 passed, 2 skipped`, source_trust file
  contributes 11 passed + 1 skipped).
- Frozen: `scripts/validate_two_node_docker_source_trust.py`,
  `scripts/validate_two_node_docker_runtime.py`,
  `.github/workflows/ci.yml`, the #1211 meta-guard test, the #1128
  Class C test (`:4424-4448`).

Seams under test (upstream-declared, consumed not renegotiated): the
#1128 fix shape (try/finally with best-effort rmtree) as the family
convention; `shutil.rmtree(..., ignore_errors=True)` chosen over the
merged shape's bare rmtree deliberately (the bare form can raise
FileNotFoundError from the finally when the ACTION failed before
creating the tree, masking the original error — the acceptance's
no-swallow clause forbids that); the skipif guards as the platform
gate (frozen, #1127/PR #1208).

Non-goals: shared scratch_evidence_root fixture refactor; hermetic
conversion; fixed-path concurrency; `/scratch/.../nwm-test/` top
level reclamation; any production-script change.

Minimal mergeable slice: both wraps together (they are the complete
remaining family; shipping one would leave the issue half-closed).

## 1. The two wraps

- [x] 1.1 `tests/test_two_node_docker_source_trust.py`: add
  `import shutil` to the stdlib import block (alphabetical: after
  `import shlex`, before `import subprocess`; ruff `I` enabled);
  wrap `:80-97` (the `_run_preflight` call through the last
  assertion) in `try:`, replace `:98-104` with
  `finally: shutil.rmtree(evidence_root.parent,
  ignore_errors=True)`. `evidence_root` stays bound BEFORE the try
  (`:78`). No other line of the file changes.
- [x] 1.2 `tests/test_two_node_docker_runtime.py`: pre-declare
  `written_path: Path | None = None` before the try; wrap
  `:3768-3776` (the `write_static_report` call through the last
  assertion) in `try:`, replace `:3777` with a finally of the form
  `with contextlib.suppress(OSError):
  (written_path or report_path).unlink(missing_ok=True)` —
  `report_path` is bound at `:3765` before the try;
  `written_path` is the resolved output (`_resolve_output_path`)
  and may differ from the literal only through symlink components,
  so prefer it when bound; the `suppress(OSError)` is required
  because `missing_ok` does not cover
  `IsADirectoryError`/`PermissionError` when the fixed path is
  occupied by an unexpected directory — a raising finally would
  mask the original AssertionError. Add `import contextlib` to the
  stdlib import block (alphabetical: after `import ast`, before
  `import json`). No other line of the file changes.

## 2. Spec + validation

- [x] 2.1 Spec delta: ADDED requirement in `two-node-docker-runtime`
  — scoped to the TWO named tests (source_trust explicit-run +
  runtime explicit-evidence-run-id): they SHALL clean up on failure
  paths as well as success via non-raising forms
  (`rmtree(..., ignore_errors=True)` / suppressed
  `unlink(missing_ok=True)`), without masking the original failure
  signal; explicit Class C carve-out (keeps its #1128 contract, no
  widening to future tests); 3 scenarios (assertion-failure cleanup;
  no-swallow; green-path completeness).
- [x] 2.2 `openspec validate source-trust-scratch-cleanup-hygiene
  --strict --no-interactive` green.

## Evidence Floor

- [x] E1 Paired failure-path discrimination proof (scratchpad
  replica per ORACLE ROUTING above — whitelisted `artifacts` roots,
  POST-write forced-false assertion, file-exists anti-vacuity
  precondition, body taken from the SHIPPED function via
  `inspect.getsource` + root/assertion text substitution): fixed
  body → pytest red shows the ORIGINAL `AssertionError` (not a
  cleanup `OSError`) AND the evidence tree is gone; the PRE-fix
  trailing-cleanup shape of the same replica → red AND the report
  files SURVIVE. Both outputs pasted, for BOTH tests (runtime
  pairing: suppressed `(written_path or report_path).unlink(
  missing_ok=True)` finally vs trailing bare unlink). The replica
  MUST actually EXECUTE (not skip): the harness strips/neutralizes
  the `@pytest.mark.skipif` decorator captured by
  `inspect.getsource` and injects the module-level helpers the body
  needs (`_make_checkout`/`_run_preflight`/`_current_owner`;
  `docker_runtime`) — a SKIP outcome is a FAILED proof, never
  counted as the red arm.
- [x] E2 macOS suite parity: `uv run pytest -q
  tests/test_two_node_docker_source_trust.py
  tests/test_two_node_docker_runtime.py` before (at master
  8553f9ca) and after — identical counts (`436 passed, 3 skipped`
  combined, measured at 8553f9ca this session), the two wrapped
  tests still SKIP with unchanged reasons (`-rs` output pasted).
- [x] E3 `uv run ruff check .` green; openspec strict green.
- [x] E4 Surface check: `git diff master...HEAD --name-only` = the
  2 test files + this openspec change, nothing else; frozen
  surfaces zero diff via the branch-scoped form.
- [x] E5 CI `Unit Tests` green on the PR head — the accepted Linux
  oracle actually EXECUTES both wrapped tests (ci.yml provisions
  `/scratch/frd_muziyao`), covering the issue's success-path
  node-22 items: un-skipped run, real write, green, cleanup.
