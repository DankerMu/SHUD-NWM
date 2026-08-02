# Tasks: capture-argv-identity-anchor

Fixture level: expanded · Repair intensity: standard · Issue #1259

Review record: fixture review round 0 → REVISE (1 P1 + 2 P2 +
2 notes, all with precise folds); repair iteration 1 folded F1
(supervisor-test blast radius → task 4(c) + task 6 diff-stat +
stop clause), F2 (run_capture_step negatives + dual-site red
proof), F3 (seam-abbrev bound >= 3, premise recorded), F4
(EXPECTED_CAPTURE_SCRIPT literal pin), F5 (supervisor rejection
scenario in spec delta). No design change — reviewer confirmed
all line anchors, e2e restructure safety, and mutation-sha scan
implementability.

Triage note: M — two facets over three code surfaces (verifier +
supervisor + test fixture/e2e), fully local hermetic. All design
choices are explorer-measured, not assumed: EXPECTED_REPO_PATH
(live_evidence.py:84) constrains plan.repo_path/preflight/authz and
COMMAND argv only — captures are bound to nothing (:990-1024 has
only _concrete_argv :597-606 + the #1250 seam scan :1014-1016);
plan_author builds ALL twelve capture argvs as
[capture_python, capture_script, "--kind", kind, *capture_common,
*extra] (plan_author.py:238, loop :227-241, capture_common
:214-225 with --mutation-head-sha at fixed offset), production
defaults capture_python=sys.executable (:97) and
capture_script=/home/nwm/NWM/scripts/…capture.py (:40, runbook
tier-node27 :1034-1038 uses no overrides); the hermetic e2e's
plan_prod uses production paths for repo_path+commands but
ROOT-based capture_script (tests:4986) — legal only because no
capture pin exists, so the verifier pin forces the e2e to move its
capture divergence to plan_exec (the #1250 rewrite :5073-5088
already maps ledger argv back to plan_prod); capture.py's parser
flags starting with --s are --systemctl/--schema-dump-host/
--schema-dump-container/--self-test-* (parser :755-779), so the
--se abbreviation domain collides with nothing legitimate.
Decisive traps: (1) the e2e must keep EXECUTING the real capture
script while plan_prod claims the production path — get the swap
direction wrong and either the verifier rejects the bundle or the
state machine runs a nonexistent /home/nwm path; (2) the
supervisor anchor must stay suffix-based or hermetic execution
breaks (capture tests :664 validate seam plans; e2e validates
plan_prod AND executes plan_exec); (3) `_bundle()` powers ~90 call
sites — its argv fix must keep every non-argv assertion untouched.
Risk axes: (1) IDENTITY REJECTION — printf/rogue-binary/kind-swap/
sha-mismatch bundles are EvidenceError before any PASS; (2)
ABBREV-PROOF SEAM GATE — every argparse-acceptable abbreviation of
a lone seam falls in the rejection domain, no seam-count premise;
(3) EXECUTOR COMPATIBILITY — supervisor accepts hermetic plans
(ROOT-based script, seam tokens) while binding script suffix +
kind; (4) FROZEN SURFACES — capture.py, plan_author.py,
bundle_author.py, schemas/**, tests/test_…_capture.py zero diff;
#1250 seam gate/tests byte-identical in behavior (the scan is
widened in place, its message for full-prefix tokens unchanged).

Line anchors (explorer-verified at master post-#1258):
live_evidence.py — EXPECTED_REPO_PATH :84, SELF_TEST_SEAM_PREFIX
:83, _concrete_argv :597-606, command exact pin :609-735
(expected_executable :610-620), captures loop :990-1024 (concrete
:1009, seam scan :1014-1016), ledger↔plan equality :1268,
_invocation_execution_identity :278-305 (call-site-dead, do not
touch). supervisor.py — EXPECTED_REPO :67, _assert_exact_argv :317,
_assert_concrete_argv :470-484, command exact=True :557, capture
sites :593 (kind in scope :582) and :892 (kind :890). plan_author.py
— DEFAULT_REPO :36, DEFAULT_CAPTURE_SCRIPT :40, capture_python
default :97, capture_common :214-225, argv template :238, loop
:227-241, schema_dump_list extra :229-233. capture.py — parser
:755-779 (flags: --kind required :757, then --database
--mutation-head-sha --repo --container --evidence-dir --psql
--systemctl --docker --journalctl --git --schema-dump-host
--schema-dump-container, seams :773/:778). tests/…live_evidence.py
— _bundle :497 (capture argv comprehension :1150-1158, printf
:1154), PASS anchors :1626-1630/:1704-1711, e2e :4901 (plan_prod
:4979-4991, capture_script override :4986, cleanup --repo :4993-
5001, plan_exec deepcopy :5009, command stub swap :5010-5015, seam
appends :5016-5025, fidelity :5063-5071, ledger rewrite :5073-5088,
run-plan write :5089-5090, verify+PASS :5092-5105),
_inject_capture_seam :5125-5144, seam-rejection tests after it.

Must preserve:
- `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_plan_author.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `schemas/**`, `tests/test_node27_timeseries_compression_capture.py`:
  zero diff.
- #1250 seam gate semantics for full-prefix tokens: a token starting
  with `--self-test-` is still rejected with a message containing
  the token; the three #1250 negative tests and the parser
  introspection test stay green UNMODIFIED.
- The e2e still asserts PASS_VERDICT + qualifies_task_4_5 + all
  #1250 fidelity pins; assertion count never decreases.
- All existing `_bundle()`-driven tests: only the capture argv
  construction line(s) may change inside `_bundle`; every assertion
  in every consuming test stays untouched and green.
- supervisor command-side validation (:557 exact) untouched; no
  seam check added to supervisor (recorded #1250 decision).
- `free_bytes >= MIN_FREE_BYTES` gates untouched.

## Implementation tasks

- [x] 1. Verifier identity anchor —
  `scripts/node27_timeseries_compression_live_evidence.py`, inside
  the captures loop (:990-1024), after `_concrete_argv`:
  (a) module constant `EXPECTED_CAPTURE_SCRIPT =
  f"{EXPECTED_REPO_PATH}/scripts/node27_timeseries_compression_capture.py"`;
  reject unless `len(argv) >= 4 and argv[1] ==
  EXPECTED_CAPTURE_SCRIPT` (EvidenceError naming the offending
  argv[1] and the expected path);
  (b) reject unless `argv[2:4] == ["--kind", kind]` (the loop's
  kind variable; message names both); the plan's mutation head SHA
  is available in scope (`mutation_head_sha` parameter of the
  enclosing function :913, already used :939/:988);
  (c) position-independent scan: the token `--mutation-head-sha`
  MUST appear with a following value equal to the plan's mutation
  head SHA (the field the verifier already validates on the run
  plan — reuse that exact variable/field, do not re-derive);
  missing pair or mismatched value → EvidenceError naming the
  expected sha. Handle the `--mutation-head-sha=SHA` single-token
  form too (same split("=", 1) treatment as task 2) so the
  equality cannot be dodged by form.
  argv[0] deliberately unpinned — one-line comment recording the
  decision (sys.executable is an environment fact, not a committed
  identity).
- [x] 2. Facet B widened seam scan — same loop, replacing the #1250
  scan body in place: for each token compute
  `base = token.split("=", 1)[0]`; reject when
  `base.startswith(SELF_TEST_SEAM_PREFIX)` OR when
  `len(base) >= 3 and SELF_TEST_SEAM_PREFIX.startswith(base)`
  (bases `--s` … `--self-test-`: every prefix argparse could accept
  as an abbreviation of a lone seam — bound is >= 3 per
  fixture-review F3 so even `--s` is rejected WITHOUT relying on
  `--systemctl`/`--schema-dump-*` keeping it ambiguous, the exact
  class of unrecorded premise facet B exists to kill; no legitimate
  plan token is ever `--s`, plan_author emits full flags only).
  Keep the rejection message for full-prefix tokens EXACTLY as
  #1250 shipped it (its tests pin the token-in-message contract);
  the abbreviation branch may share the message shape. One comment
  noting the measured collision facts: no legitimate capture flag
  starts with `--se`, and the `--s` rejection is premise-free.
- [x] 3. Supervisor anchor —
  `scripts/node27_timeseries_compression_supervisor.py`: one shared
  helper (e.g. `_assert_capture_producer_argv(argv, *, kind)`)
  called from BOTH :593 (validate_run_plan) and :892
  (run_capture_step) after `_assert_concrete_argv`: require
  `len(argv) >= 4`, `argv[1].endswith("/scripts/node27_timeseries_compression_capture.py")`,
  `argv[2:4] == ["--kind", kind]`; SupervisorError (the file's
  existing error type at those sites) naming the violation. Suffix
  not EXPECTED_REPO — comment records the executor-vs-verifier
  asymmetry. NO seam logic here. KNOWN BLAST RADIUS (fixture review
  round 0, reviewer-measured): ~10 tests in
  tests/test_node27_timeseries_compression_supervisor.py break —
  `_plan()` builds every capture argv as
  `[sys.executable, "-c", "print('{}')"]` (:244-248), failing both
  new checks; affected: validate_run_plan positives :297/:377/:1190
  (+ :1153 via main), run_capture_step sites :527/:539 (its
  "exists before its owner" match), :2617/:2643 (HardWallExpired),
  :2670, state-machine tests :577/:2752 with argv rewrites
  :566-569/:2723-2726 and `_finite_writer` :2530-2537 — handled by
  task 4(c), NOT by weakening the anchor.
- [x] 4. Test fixture + e2e mechanical updates
  (tests/test_node27_timeseries_compression_live_evidence.py):
  (a) `_bundle()` capture argv comprehension (:1150-1158) becomes
  producer-shaped:
  `[sys.executable, evidence.EXPECTED_CAPTURE_SCRIPT, "--kind", kind,
  "--mutation-head-sha", <the sha literal _bundle already embeds in
  the plan>]` — reuse the existing sha variable, do not invent a
  second one; every other `_bundle` field untouched;
  (b) e2e: remove the `capture_script=` override (:4986) so
  plan_prod captures carry the production default path
  (capture_python stays default — argv[0] unpinned); at the
  plan_exec divergence point (:5009-5025) swap each capture's
  argv[1] to `str(ROOT / "scripts/node27_timeseries_compression_capture.py")`
  (alongside the existing seam appends); the #1250 ledger rewrite
  (:5073-5088) already restores plan_prod argv — verify no other
  site compares plan_exec capture argv to plan_prod;
  (c) `tests/test_node27_timeseries_compression_supervisor.py`
  mechanical updates (fixture-review F1): (i) `_plan()`'s capture
  argv template (:244-248) becomes producer-shaped —
  `[sys.executable, "/home/nwm/NWM/scripts/node27_timeseries_compression_capture.py", "--kind", kind, ...]`
  (validate-only plans never execute the argv, so the production
  path is fine); (ii) every test that actually EXECUTES a capture
  (run_capture_step :527/:539/:2617/:2643/:2670, state-machine
  :577/:2752 via argv rewrites :566-569/:2723-2726,
  `_finite_writer` :2530-2537) writes its stub script to a file at
  `<tmp>/scripts/node27_timeseries_compression_capture.py` and
  passes `[sys.executable, str(stub), "--kind", <kind>, ...]` so
  the suffix+kind anchor passes and the ORIGINAL failure signature
  is preserved. STOP CLAUSE: the fail-closed tests must keep
  raising `HardWallExpired` / matching "exists before its owner" —
  never re-point their `pytest.raises` at the new SupervisorError;
  if a signature cannot be preserved, stop and report.
  (d) `_inject_capture_seam` (:5125-5144) and the #1250 negative
  tests operate on `_bundle()`-derived plans — after (a) they must
  still reach the seam scan (identity checks pass first); confirm
  and adjust nothing unless red proves otherwise (stop clause: if
  the seam tests start failing for identity reasons, fix the
  fixture shape, never the gate order).
- [x] 5. New tests (append-only):
  (a) printf bundle — a `_bundle()` variant with one capture's argv
  reverted to `["/usr/bin/printf", "{}"]` → EvidenceError naming
  the expected producer script (the pre-#1259 PASS-shaped smoking
  gun, now structurally dead);
  (b) kind-swap — capture argv otherwise valid but `--kind` value
  of a DIFFERENT kind → EvidenceError naming both kinds;
  (c) mutation-sha — (i) pair missing, (ii) value mismatched,
  (iii) `--mutation-head-sha=WRONG` form → all EvidenceError naming
  the expected sha;
  (d) abbreviation — token `--self-t` (with value) in a capture
  argv → EvidenceError (facet B; kills the "two seams collide"
  hidden premise); also `--se` alone rejected;
  (e) supervisor negatives at BOTH sites — `validate_run_plan`
  refuses a plan whose capture argv[1] lacks the script suffix, and
  one whose `--kind` mismatches; AND `run_capture_step` refuses the
  same two shapes (fixture-review F2 — the :892 site needs its own
  negatives, else an implementer wiring the helper into
  validate_run_plan only ships checkbox 2 half-met while every
  other oracle stays green); positive control: the hermetic-shaped
  plan (ROOT-based argv[1], seam tokens appended) still validates
  AND executes;
  (f) collision structural test — introspect capture.py's parser
  (same pattern as #1250's test): NO flag outside the two seams has
  an option string starting with `--se` (pins the facet-B rejection
  domain's zero-collision fact against future flags); plus the
  anti-tautology literal pin (fixture-review F4):
  `evidence.EXPECTED_CAPTURE_SCRIPT ==
  "/home/nwm/NWM/scripts/node27_timeseries_compression_capture.py"`
  asserted as a LITERAL string (a mis-derived constant — e.g. from
  REPO_ROOT :87 instead of EXPECTED_REPO_PATH :84 — passes every
  other test because gate and fixture share the constant; the e2e
  catches it only via plan_author.DEFAULT_CAPTURE_SCRIPT, a
  load-bearing coupling this pin makes explicit).
- [x] 6. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_supervisor.py` all green
  (record before/after counts per file; capture file count
  unchanged); red proofs via TWO stash cycles: (i) stash the
  live_evidence.py hunk → 5(a)-(d) fail (PASS-or-wrong-error
  signature), supervisor negatives 5(e) stay green, #1250 seam
  tests stay green (their tokens still hit the OLD scan — state
  what actually happens and record it); (ii) stash the
  supervisor.py hunk → 5(e) negatives fail AT BOTH SITES (at least
  one validate_run_plan red AND one run_capture_step red —
  fixture-review F2), verifier tests stay green; `uv run ruff
  check .`; `openspec validate capture-argv-identity-anchor
  --strict --no-interactive`; `git diff --stat` → exactly
  live_evidence.py + supervisor.py + the live_evidence test file +
  the supervisor test file (+ this fixture).

## Required evidence

- Red-then-green for every 5(a)-(e) case with the correct stash
  attribution (verifier reds under stash-i, supervisor reds under
  stash-ii); collision test 5(f) green independent of both stashes
  (stated); before/after test counts for all three files;
  zero-diff statement for capture.py/plan_author/bundle_author/
  schemas/capture-test-file; #1250 seam tests + parser test
  unmodified and green; e2e PASS retained with production-path
  plan_prod captures (diff hunks cited for the swap direction);
  ruff; the `_bundle` argv change shown as a single-comprehension
  hunk.

## Non-goals

- capture.py `allow_abbrev` change, argv[0]/full-layout pinning,
  output fingerprinting, schema changes, #1240's dead
  `_invocation_execution_identity` (all per proposal).
