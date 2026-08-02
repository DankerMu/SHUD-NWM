# Tasks: guard-capture-docker-record-exec

Fixture level: compact · Repair intensity: light · Issue #1090

Triage note: S, single-file production change + two test-site argv
patches + two new tests; fully locally verifiable (hermetic stub
docker), no node-27 live receipt needed. Risk axes: (1) the assertion
MUST run before ANY subprocess call or bundle-content production in
`_capture_schema_dump_list` — NOTE the stdout-empty/evidence-empty
assertions alone cannot distinguish placement (the document is emitted
only after the function returns, at main() :781), so the negative
test's deviating docker stub MUST write a marker file when invoked and
the test MUST assert the marker is absent — that is what actually
covers the spec's "before running any subprocess" clause; (2)
plan_author MUST NOT learn to emit the seam flag (see proposal change
2 — auto-emission would nullify the guard for future production
callers); the two test sites post-patch argv instead, mirroring the
`--self-test-free-bytes` threading (live_evidence tests :4991-4993);
(3) captures are structurally excluded from exact-argv gates
(supervisor `_assert_exact_argv` covers only `commands` kinds;
captures get `_assert_concrete_argv` without `exact=True` at :593/:892), so the
appended flag breaks no gate — but `_assert_concrete_argv` requires
absolute executable paths and rejects shell-template chars; a plain
`--self-test-docker-seam` token passes; (4) the negative test must
inject its deviating docker through the SAME subprocess entrypoint the
other capture tests use (`_run_capture` helper, capture tests
:288-322) — the helper hardcodes `--docker` at :310-311 and asserts
`check=True` + empty stderr, so extend it with a `docker: str | None`
override parameter and an `expect_failure` mode returning the
`CompletedProcess` instead of relying on argparse last-wins duplicate
flags; record exit code, stderr excerpt, stdout emptiness, and
evidence-dir created-but-empty (main() :765 mkdirs it BEFORE dispatch;
documents are emitted on stdout via `_emit` :203-209, never as files —
an "output file absent" assertion would be vacuous); (5) `--help`
visibility: use
`help=argparse.SUPPRESS` like `--self-test-free-bytes` (:755) so the
production CLI surface is unchanged. Single review round.

Must preserve:
- `version_argv`/`list_argv` recorded content byte-identical
  (HOST_DOCKER_CLI-based, :514-515); verifier literal pins
  (live_evidence.py:1616-1623) untouched.
- `HOST_DOCKER_CLI` value; `--docker` required seam; plan_author.py
  untouched (zero diff); schemas/** untouched; supervisor script and
  verifier script untouched.
- Production path: `ctx.docker == HOST_DOCKER_CLI` runs with NO seam
  flag and zero behavior change.
- Baselines green at master 4538a60b: capture 11, live_evidence 277,
  supervisor 127 (collect-only 127; suite untouched).

## Implementation tasks

- [x] 1. capture.py: add `--self-test-docker-seam` (`action="store_true"`,
  `help=argparse.SUPPRESS`) beside `--self-test-free-bytes` (:755);
  `Context.self_test_docker_seam: bool = False` beside :117 with the
  same test-only docstring style; wire `main()` Context construction
  (:779 area). No validation needed (boolean).
- [x] 2. capture.py `_capture_schema_dump_list`: first statement —
  `if ctx.docker != HOST_DOCKER_CLI and not ctx.self_test_docker_seam:
  raise CaptureError(f"--docker deviates from HOST_DOCKER_CLI without
  --self-test-docker-seam: {ctx.docker}")` (exact message wording may
  be adjusted; MUST contain the observed value). Update the :504-511
  comment to state the enforced invariant.
- [x] 3. Test-site argv patches (the ONLY places the seam is enabled):
  (a) capture tests `test_authored_plan_survives_the_real_state_machine
  _and_verifier_validators` — after `build_run_plan(...)`, append the
  flag to the `schema_dump_list` capture argv (post-hoc patch, same
  style as live_evidence :4991-4993); (b) live_evidence merged
  G-series e2e (:4977-5042) — same patch on `plan_prod`, placed INSIDE
  the :4991-5001 capture-patching loop, i.e. BEFORE the
  `run_plan_id` recompute at :5002 AND before the `deepcopy` at :5007
  (the verifier binds ledger capture argv to plan capture argv at
  live_evidence.py:1253, and plan id/content binding at :930).
  plan_author.py itself: zero diff.
- [x] 4. NEW negative test (capture tests): invoke the capture
  producer for kind `schema_dump_list` with a RUNNABLE stub docker at
  a second stub dir (path != /usr/bin/docker, real
  `--schema-dump-host`/`--schema-dump-container` supplied) and NO
  seam flag → assert non-zero exit, stderr contains both the flag
  name and the observed docker value, stdout is EMPTY (no JSON
  document emitted), the `--evidence-dir` directory exists but
  contains no files, AND the stub was never invoked: the deviating
  stub writes a marker file on invocation and the test asserts the
  marker is absent (risk axis 1 — this is the only assertion that
  covers "before running any subprocess"). Extend `_run_capture` with
  `docker: str | None` and `expect_failure` (returns CompletedProcess)
  per risk axis 4.
- [x] 5. NEW plan_author guard test (capture tests): with production
  defaults, `build_run_plan(...)` emits NO capture argv containing
  `--self-test-docker-seam`, and the `schema_dump_list` capture argv
  contains `--docker` followed by `/usr/bin/docker` — pins the
  "plan_author never emits the seam" invariant (issue AC 4; makes the
  spec's production-default scenario testable). Review round 1
  (verifier CONFIRMED): the production-default plan alone is blind to
  the named regression (deviation-conditional auto-emission never
  fires under defaults), so the SAME test also builds a plan with a
  deviating `capture_docker` and asserts no capture argv carries the
  seam flag — red under the auto-emission mutation, green at HEAD.
- [x] 6. Red proof (scratch mutation, restored, outputs recorded):
  comment out the new assertion → the negative test goes red because
  the producer now EXITS 0 and emits on stdout a document whose
  `version_argv`/`list_argv` attest HOST_DOCKER_CLI while the runnable
  stub at the other path actually executed — the false-attestation
  reproduced verbatim (this is why task 4 requires a RUNNABLE stub: a
  nonexistent binary would die in `_run` :141-142 with a different
  CaptureError and never produce the false bundle); restore → green.
  Record the false bundle excerpt.
- [x] 7. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_live_evidence.py` → green
  (capture 11→14: +1 negative +1 plan_author guard +1 scenario-3
  pass-through; live_evidence 277 unchanged); `uv run pytest -q
  tests/test_node27_timeseries_compression_supervisor.py` → 127
  (regression, untouched); `uv run ruff check .`; `git diff --stat` →
  capture.py + 2 test files (+ fixture); plan_author/verifier/schemas
  zero diff; `openspec validate guard-capture-docker-record-exec
  --strict --no-interactive`.
- [x] 8. Review round 1 fix pass (2× verifier-CONFIRMED coverage
  gaps): (a) extend task-5 test with the deviating-caller case (see
  task 5); (b) NEW in-process scenario-3 test — `Context` with
  `docker == HOST_DOCKER_CLI`, no seam, `schema_dump_host=None` →
  `_capture_schema_dump_list` raises the PRE-EXISTING
  "requires --schema-dump-host/--schema-dump-container" CaptureError
  and never mentions the seam flag, proving the guard passes through
  on the pinned path (red under an over-broad
  `if not ctx.self_test_docker_seam` mutant); (c) rescope the
  RECORD/EXEC comment's final sentence to this capture kind + no-seam
  condition. Deferred with routing: seam invisibility to downstream
  verifier gates (fixture non-goal) → follow-up issue #1250.

## Required evidence

- Negative-test output (exit code, stderr excerpt with observed
  value, stdout emptiness, evidence-dir created-but-empty proof);
  red-proof false-attestation bundle excerpt (stdout document with
  HOST_DOCKER_CLI-attesting argvs under a deviating runnable stub)
  with the assertion disabled; plan_author guard test output; pytest
  counts (14/277/127); ruff; zero-diff proof for plan_author.py.

## Non-goals

- Seam removal; HOST_DOCKER_CLI/plan_author/verifier/schema changes;
  both-argvs-recorded alternative; other seams; #1088/#1089 surfaces.
