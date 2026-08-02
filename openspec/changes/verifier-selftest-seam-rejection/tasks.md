# Tasks: verifier-selftest-seam-rejection

Fixture level: compact · Repair intensity: light · Issue #1250

Triage note: S — one verifier-side check + one e2e restructure using
the test's own established rewrite pattern + four new tests; fully
local hermetic. Fixture review round 0: ACCEPT-with-tightenings, all
folded — 3 P2 (authored-plan-survives range corrected to :627-810
naming its document-level validator calls; prefix semantics pinned
by new negative test 3(d) with an unregistered prefix token, killing
an enumerated-two-token implementation; fidelity assertion extended
to prove the free-bytes seam is HONORED via snapshot free_bytes ==
_SELFTEST_FREE_BYTES, not merely present in argv) + 3 notes (spec
delta moved into the existing hypertable-compression capability
beside its sibling requirements; the plan_prod loop's load-bearing
--repo rewrite at :5003-5009 named as must-stay; the known accepted
cost — verified-capture-argv != executed-capture-argv, mirroring the
command side — recorded in the proposal so later review rounds do
not re-litigate it). Reviewer verified all line anchors against the
worktree and confirmed the rewrite mechanics four ways (ledger has
no hash chain; run_plan_id binds to plan_prod only; no hidden argv
consumer; capture events keyed by capture_id shared across the
deepcopy). The issue's mandatory pre-implementation design
tension (how the hermetic e2e keeps PASS coverage once the verifier
rejects seams) is RESOLVED by explorer evidence, not assumption:
the merged G-series e2e really subprocess-executes capture.py (so
seams must stay on the EXECUTION side), and the same test already
post-hoc rewrites command-side ledger identities (:5057-5062) — the
capture side gets the identical treatment (seam-free plan_prod,
seams only in plan_exec, ledger rewritten back, fidelity asserted
pre-rewrite). Supervisor-side gate REFUSED as structurally
incompatible with hermetic execution (capture tests :627/:664 calls
validate_run_plan with the seam present, and the executor must run
seam-carrying plans). The decisive trap is vacuity: the restructured
e2e could silently stop executing seams (hermetic fidelity lost) or
the rewrite could become a no-op — both are pinned by explicit
assertions. Risk axes: (1) STRUCTURAL REJECTION — a seam token in
run_plan capture argv makes PASS impossible (EvidenceError before
any PASS path), covering both known seams and, via prefix, every
future `--self-test-*` flag; (2) HERMETIC FIDELITY — the e2e still
really executes capture.py with the seams (assert executed ledger
argv carried them before the rewrite); (3) NO NEW INVISIBLE SEAM —
no verifier acceptance flag, no supervisor change, ledger covered
transitively by the :1253 equality binding (a ledger-only seam
fails equality; a plan seam fails the new gate); (4) FROZEN
SURFACES — capture.py, supervisor.py, plan_author.py, bundle_author,
schema all zero-diff; every existing test except the one
restructured e2e stays byte-identical.

Line anchors (orchestrator/explorer verified at master b05c6537):
capture.py — HOST_DOCKER_CLI :54, docker-seam guard :511-514,
recorded-vs-exec argv :530-535, `--self-test-free-bytes` argparse
:773, `--self-test-docker-seam` argparse :778, free-bytes validation
:786-787. live_evidence.py — EvidenceError alias :300, _concrete_argv
:589-598, _validate_supervisor_execution :904 (captures loop :984-
1007, capture argv check :1001, ledger↔plan equality :1251-1259),
literal version_argv pin :1616-1623, MIN_FREE_BYTES :61 gates :1862
:2017, verify_bundle :2960 (calls _validate_supervisor_execution
:3041). supervisor.py — _assert_concrete_argv :470-484, command
exact :557, capture concrete-only :593 and :892, executed argv into
ledger :931. e2e — test_real_state_machine_bundle_verifies_task_4_5_pass
:4901-5079; seam injection into plan_prod :4991-5009 (free-bytes
:4993, docker-seam :5001); run_plan_id :5010; validate :5011;
plan_exec deepcopy :5015; command stub swap via _e2e_child_argv
:4871-4898; state machine :5041; command-side ledger rewrite
:5057-5062; build_bundle :5066; verify :5077. capture tests —
deviating-docker refusal :475, plan-author-never-emits :529-559,
pinned-host passes :562, authored-plan-survives :627-810 (runs to
end of file; seam injection :664; calls only document-level
verifier validators — `evidence._validate_preflight` :711,
`_catalog_snapshot` :718, `_validate_d3_catalog` :719 — never
`verify_bundle`/`_validate_supervisor_execution`, so the new gate
does not touch it).

Must preserve:
- `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_supervisor.py`,
  `scripts/node27_timeseries_compression_plan_author.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `schemas/timeseries_compression_live_evidence.schema.json`: zero
  diff.
- `tests/test_node27_timeseries_compression_capture.py`: zero diff
  (all four seam-related tests stay green unmodified — explorer
  verified none calls verify_bundle).
- In `tests/test_node27_timeseries_compression_live_evidence.py`:
  every test except `test_real_state_machine_bundle_verifies_task_4_5_pass`
  byte-identical; the restructured e2e still asserts
  `result["verdict"] == evidence.PASS_VERDICT` plus strictly MORE
  assertions than before (fidelity pin), never fewer.
- `free_bytes >= MIN_FREE_BYTES` gates (:1862/:2017) untouched —
  the argv-level rejection is additive, not a substitute.

## Implementation tasks

- [x] 1. `scripts/node27_timeseries_compression_live_evidence.py`:
  add module constant `SELF_TEST_SEAM_PREFIX = "--self-test-"` near
  the other constants, and in `_validate_supervisor_execution`'s
  captures loop — at the point where each capture's argv has just
  passed `_concrete_argv` (:1001) — reject any token
  `token.startswith(SELF_TEST_SEAM_PREFIX)` with
  `raise EvidenceError(f"run plan capture argv carries a self-test
  seam token: {token}")` (adjust to the file's exact lower-case
  message style; message MUST contain the literal offending token).
  Check the PLAN capture argv only; do not add a ledger-side twin
  (the :1253 equality binding covers it — record this reasoning in
  a one-line comment at the check site). No other logic changes.
- [x] 2. Restructure `test_real_state_machine_bundle_verifies_task_4_5_pass`
  (:4901): (i) remove ONLY the seam appends from the plan_prod loop
  (:4991-5009) — the loop carries one load-bearing non-seam
  mutation that MUST stay: the cleanup capture's `--repo` rewrite
  to `str(ROOT)` at :5003-5009 (`_validate_reviewed_file_ref`
  depends on it) — so `plan_prod` and its `run_plan_id` (:5010) are
  seam-free; (ii) after
  `plan_exec = copy.deepcopy(plan_prod)` (:5015), append the same
  seam flags to `plan_exec["captures"][*].argv` (free-bytes flags on
  the two selection captures, docker-seam on schema_dump_list —
  exactly the tokens previously injected at :4993/:5001); (iii)
  after the state machine runs and BEFORE the bundle is built,
  assert hermetic fidelity BOTH ways: the ledger capture events'
  argv (as actually written by supervisor :931) contain the seam
  tokens for those three captures, AND the seams were HONORED — the
  two produced selection snapshots' `free_bytes` equal
  `_SELFTEST_FREE_BYTES` (tests :4432); without the value pin, a
  capture that stops honoring `--self-test-free-bytes` passes
  silently on any runner with >300 GiB free (docker-seam needs no
  such pin — dropping it crashes the run at capture.py:511);
  (iv) extend the existing post-hoc ledger
  rewrite (:5057-5062 command pattern) to rewrite each ledger
  capture event's argv to the seam-free `plan_prod` capture argv
  (match by capture kind/identity the same way the plan binds);
  (v) keep `assert result["verdict"] == evidence.PASS_VERDICT` and
  all existing assertions. If the state machine or bundle author
  turns out to bind capture events to plan_exec argv in a way the
  rewrite cannot satisfy, STOP and report the blocker — do not
  weaken any verifier check or existing assertion to force green.
- [x] 3. Tests (append-only apart from task 2's restructure):
  (a) negative docker-seam — build/obtain a verifying bundle (reuse
  the e2e's helpers or the smallest PASS fixture already used by
  neighboring tests), inject `--self-test-docker-seam` into ONE
  run-plan capture argv AND its equality-bound ledger event, run
  `verify_bundle` → `EvidenceError` whose message contains
  `--self-test-docker-seam`; assert it is NOT a PASS and NOT the
  equality-binding error (the seam gate must fire, i.e. message
  contains the token — this kills an implementation that only
  trips on argv inequality);
  (b) negative free-bytes — same shape with
  `--self-test-free-bytes` + a value token appended to a selection
  capture argv (both plan and ledger sides), message contains
  `--self-test-free-bytes`;
  (c) structural registration — import capture.py's parser factory
  (or construct the parser the way its main() does), iterate
  `parser._actions` (or equivalent public introspection), and
  assert every optional flag whose help is `argparse.SUPPRESS` has
  all its option strings start with `--self-test-` — a future
  hidden flag outside the prefix reddens this test. Also assert the
  two known seams are present in that set (guards against the
  parser moving/renaming making the test vacuous). Scope is
  capture.py's own parser only (the unrelated SUPPRESS'd
  `--selectors` in scripts/node27_db_export_salvage.py:306 is not
  in it);
  (d) prefix semantics — inject a token that is NOT one of the two
  known seams but matches the prefix (e.g.
  `--self-test-unregistered-probe`) into a plan capture argv + its
  equality-bound ledger event → `EvidenceError` whose message
  contains that token. This kills an implementation that rejects
  only an enumerated two-token set, which would let the next
  registered-prefix seam sail through the verifier — the exact
  leak-by-forgetting hole the issue's acceptance criterion 3
  targets;
- [x] 4. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_capture.py` all green
  (record before/after counts; capture file count unchanged);
  `uv run ruff check .`; `openspec validate
  verifier-selftest-seam-rejection --strict --no-interactive`;
  red proof: with the live_evidence.py hunk stashed
  (`git stash push -- scripts/node27_timeseries_compression_live_evidence.py`),
  tests 3(a)/3(b)/3(d) must FAIL — their red signature is
  `verify_bundle` returning PASS (or a different error) instead of
  the seam EvidenceError; 3(c) stays green under the stash (it
  tests capture.py's parser, not the verifier — state this
  explicitly in the evidence); the restructured e2e must be green BOTH with and
  without the stash (it never carries seams into the verifier
  anymore); `git diff --stat` → exactly live_evidence.py + the
  live_evidence test file (+ this fixture).

## Required evidence

- Red-then-green for 3(a)/3(b)/3(d) (PASS-or-wrong-error before,
  seam EvidenceError naming the token after); 3(c) green
  independent of the verifier hunk (stated); hermetic-fidelity
  assertions shown in the diff (executed ledger argv carried seams
  pre-rewrite AND snapshot free_bytes == _SELFTEST_FREE_BYTES);
  e2e PASS retained with plan_prod seam-free; zero-diff statement
  for capture.py/supervisor.py/plan_author/bundle_author/schema and
  for the capture test file; before/after test counts both files;
  ruff; move of seam injection provably from plan_prod to plan_exec
  (diff hunks cited).

## Non-goals

- Seam removal, schema changes, supervisor/capture/plan_author
  changes, other injection surfaces, exact-argv-per-capture-kind
  alternative (all per proposal).
