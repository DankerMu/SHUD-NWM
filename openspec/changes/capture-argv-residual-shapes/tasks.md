# Tasks: capture-argv-residual-shapes

Fixture level: expanded · Repair intensity: standard · Issue #1263

Review record: fixture review round 0 → REVISE (2 P1 + 2 P2 + 1 P3 +
1 note); repair iteration 1 folded F1 (spec delta gains a MODIFIED
restatement of the #1262 tool-value requirement — `--evidence-dir`
leaves its "deliberately unpinned" sentence, else the archived
capability spec self-contradicts; delta is now one ADDED + one
MODIFIED), F2 (must-preserve was unsatisfiable: the pinned
zero-collision test hardcodes tuple cardinality 8 at :5764 — carved
out as the ONLY permitted body change 8→9, and 5(g) now requires
EXTENDING that test, "accompanies" deleted), F3 (red proof widened to
THREE hunks — the `--ev`/`--e` negatives are carried by the :142
tuple element, not by either gate hunk), F4 (spec scenario split:
four-shape mismatch → evidence-dir gate message with observed +
derived expected; proper-prefix → existing pinned-abbreviation
wording, the equality gate never fires on prefixes), F5 (anchor
`_argv_option_values` :623-647 → :667-691), Note (help-message
wording made spelling-safe: `--help=x` is SystemExit(2) usage error,
not help+exit 0). Round 1 → REVISE (1 P2 + 1 P3); repair iteration 2
folded R1 (the templates must-preserve bullet now carves out touch
class (i) explicitly — all five `_producer_argv` call sites gain the
evidence_dir argument; 4(b) forbids a default) and R2 (three-hunk
red proof expects exactly ONE pre-existing red: the zero-collision
cardinality pin 9-vs-8 AssertionError, the :142 hunk's own
attribution proof, listed separately from the DID-NOT-RAISE list).

Fix round 1 (post cross-review, PR #1264 HEAD 8d72c4f7): both lenses
independently found and the verifier CONFIRMED (FIX_NOW) that the
help branch's single-dash arm matched `-h` by exact equality only,
while argparse short-option cluster parsing routes every
`-h<suffix>` token (`-hx`, `-hh`, `-hjunk`, `-help`, … — 156
enumerated) to the same auto help action: SystemExit(0), full help,
zero captures, yet verify_bundle returned PASS_TASK_4_5 — the
delivered code violated the delta's own universal SHALL sentence.
Fold: the single-dash arm becomes `base.startswith("-h")` (verifier
exhaustively probed: 0 escapes remain, 0 false positives — no
plan_author/template/e2e token begins with `-h`; `-h=x` normalizes
to `-h` unchanged; double-dash arm untouched since
`"--help".startswith("-h")` is False; `-xh`-style clusters exit 2
before help and stay outside this gate's declared scope, recorded
as the exit-2 residual family). Spec delta requirement + scenario
updated to name the cluster family; `_HELP_EARLY_EXIT_TOKENS`
extended with cluster ids; the "explicit equality case is
load-bearing" comment rationale superseded by "the single-dash
domain is rejected wholesale by `-h` prefix". Two CONFIRMED P3s
ride along: the :117 in-module self-citation of the MIN_FREE_BYTES
gate lines (stale at birth, +74 drift) goes symbolic (no line
numbers — the file's only in-module line citation); and one
trailing-slash-root positive test pins the rsplit-vs-normalizing
derivation choice (verifier probe: a `Path.parent` swap stayed
green on all 354 — the divergence the gate comment claims was
untested). Proposal-overstatement candidate REFUTED (sentence is
explicitly pre-change-scoped; spec/comment wording already matches
delivered strength).

Triage note: S — one code surface (verifier) + one test file, fully
hermetic; suggested level from the issue is p3/S but the fixture stays
expanded because the risk surface is identical to #1259/#1261 (the
forensic gate chain) and the decisive hazard is again TEMPLATE
ATTRIBUTION: adding a required relational binding without extending both
argv templates would re-attribute every existing negative to "missing
--evidence-dir" — the exact template-coverage defect class fixture
review caught in #1261. Both #1260-round-1 lessons are pre-applied at
fixture time: (1) the newly pinned option enters the proper-prefix
rejection domain in the SAME change (`--ev`/`--e` rebinds); (2) evidence
must chase the delta (PR body updated after any fix commit). Risk axes:
(1) HELP-TOKEN REJECTION — all six spellings refuse before PASS;
(2) RELATIONAL BINDING — `--evidence-dir` must equal the output_path
sibling, four failure shapes + abbreviation rebind refuse, production
and hermetic plans both still pass (relational, no literal);
(3) ATTRIBUTION PRESERVATION — every pre-existing negative still fails
on the field it corrupts (template extension, single-point);
(4) FROZEN SURFACES — capture.py, plan_author.py, supervisor.py,
bundle_author.py, schemas/**, capture + supervisor test files: zero
diff; #1250/#1259/#1262 gate code and messages byte-stable (the
PINNED_CAPTURE_VALUE_OPTIONS tuple gains one element — recorded
data-domain widening, loop code unchanged).

Line anchors (orchestrator-verified at master fed5a60a; issue-scribe
measured the same content at ec45d76a):
live_evidence.py — MIN_FREE_BYTES :61 (300 GiB),
SELF_TEST_SEAM_PREFIX :83, EXPECTED_CAPTURE_SCRIPT :89,
ANCHORED_CAPTURE_OPTIONS :98, deliberately-absent rationale :113-118
("run-scoped, varies per run" — the sentence shape 3 rewrites),
EXPECTED_CAPTURE_TOOL_VALUES :119-127, PINNED_CAPTURE_VALUE_OPTIONS
:142, _argv_option_values :667-691, _concrete_argv :655-664, capture
gates :1094-1190 (argv[0] comment :1098-1100, argv[1] identity
:1101-1106, kind positional :1107-1111, kind exactly-once :1116-1120,
sha exactly-once :1121-1125, tool-value loop :1135-1141, per-token scan
:1169-1186 — seam branch :1171-1174, anchored prefixes :1175-1180,
pinned prefixes :1181-1186), output_path validation :1086-1092, ledger
ref equality :1449, free_bytes hard gates :2046-2047 and :2201-2203.
capture.py (FROZEN) — parser :755-780 (add_help default True; no
`--h*` business flag; `--evidence-dir` :762 is the only `--e*` flag;
no single-dash flag beyond auto `-h`), main :782-806 (parse_args
first — help exits before any capture), _free_bytes :490-502
(os.statvfs(ctx.evidence_dir) :501), snapshot free_bytes :472, help
text ~2.6 KB (COLUMNS-dependent; 2585 measured in fixture review, 2651
by issue-scribe), `-h`/`--help`/`--h`/`--he`/`--hel` all
SystemExit(0) printing help; `--help=x` is SystemExit(2) usage error
with NO help printed (fixture-review executed proof) — every spelling
is a non-production early exit, but refusal-message wording must not
claim "prints help and exits 0" for the whole family.
plan_author.py (FROZEN) — evidence-dir derivation :219
(`f"{root}/capture-artifacts"`), output_path :239
(`f"{root}/capture-{kind}.json"`) — SAME `root`, so
`output_path.rsplit("/", 1)[0] + "/capture-artifacts"` is the exact
textual inverse for every plan_author-authored plan (trailing-slash
roots round-trip consistently through both f-strings; no filesystem
normalization anywhere).
tests/…live_evidence.py — _pinned_capture_options :161-180, _bundle
capture template :1172-1188 (output_path = produced_refs[kind]["path"],
all directly under tmp_path → derived value is
f"{tmp_path}/capture-artifacts" for every kind), _replace_capture_argv
:5325-5347, _producer_argv :5350-5368 (SHA stays `*extra` —
[pair_missing] red capability), _replace_produced_artifact :2811-2836
(replacement refs still under tmp_path — relation unbroken), e2e plans
derive both fields from the same tmp root via plan_author → relation
automatic, plan_exec rewrites tool values by name and touches neither
--evidence-dir nor output_path.
Blast radius: prearm plan_owned_paths reads
output_path/capture_id/kind, never argv — unaffected; supervisor
validate_run_plan has no help/evidence-dir logic (recorded asymmetry);
run_plan is an opaque artifact_ref — zero schema change.

Must preserve:
- `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_plan_author.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `scripts/node27_timeseries_compression_supervisor.py`, `schemas/**`,
  `tests/test_node27_timeseries_compression_capture.py`,
  `tests/test_node27_timeseries_compression_supervisor.py`: zero diff
  (suite baselines 14 and 141 unchanged).
- #1250 seam branch, #1259 anchored gates + messages, #1262 tool-value
  gate + messages: code and message strings byte-stable. The ONLY
  permitted edits inside :1094-1186 are (i) the new help-token branch,
  (ii) the new sixth gate, (iii) the argv[0] comment append, (iv) the
  one-element tuple extension at :142 with its comment. All existing
  message-substring assertions stay green.
- Both templates change at exactly one point each; every consuming
  TEST BODY stays zero-diff and green APART FROM touch class (i)
  below — the `_producer_argv` call lines gain the `evidence_dir`
  argument (all five call sites: :5410, :5436, :5455, :5684, :5710;
  4(b) forbids a default value, so these call-line edits are
  required, mechanical, and the ONLY in-body change) — explicitly
  including
  `test_verifier_accepts_the_inline_mutation_sha_form_when_it_matches`
  (doubles as the sixth gate's non-vacuity positive control after the
  template extension) and
  `test_verifier_rejects_capture_argv_without_the_plan_mutation_sha`
  (all four ids — `_producer_argv` keeps SHA caller-supplied).
- The relational gate applies to ALL twelve capture kinds uniformly —
  no per-kind exemption; the twelve-kind plan_author positive control
  and the e2e (PASS_VERDICT + qualifies_task_4_5 + all fidelity pins)
  stay green with zero e2e assertion decrease.
- The 28-case #1261 matrix, database mismatch, abbreviation-rebind,
  drift-guard and whole-dict-pin tests: green, with exactly two
  mechanical touch classes permitted and nothing else — (i)
  `_producer_argv` call sites gain the new `evidence_dir` argument
  (task 4(b)); (ii) the pinned zero-collision test
  `test_capture_cli_has_no_flag_abbreviating_a_pinned_capture_option`
  changes its cardinality literal `len(evidence.
  PINNED_CAPTURE_VALUE_OPTIONS) == 8` (:5764) to `== 9` — the
  data-domain widening's companion pin; every other line of these
  test bodies stays byte-identical. (`_PINNED_TOOL_OPTIONS` :5638
  derives from EXPECTED_CAPTURE_TOOL_VALUES, stays 7 — the 28-case
  matrix cardinality does not change.)
- Command-side `_validate_exact_command_argv` untouched.

## Implementation tasks

- [ ] 1. Help-token rejection —
  `scripts/node27_timeseries_compression_live_evidence.py`, per-token
  scan (:1169-1186): new branch immediately after the seam branch
  (:1171-1174), before the anchored/pinned prefix loops (domains are
  disjoint — no `--h*` anchored/pinned option exists — so ordering is
  free; grouping with the seam branch keeps the early-exit family
  together): reject when `base == "-h"` or
  `len(base) >= 3 and "--help".startswith(base)` (`base =
  token.split("=", 1)[0]` — already computed; covers `--h`, `--he`,
  `--hel`, `--help`, `--help=x`). `-h` is length 2, outside the
  `len >= 3` mechanism the other branches use — the explicit equality
  case is load-bearing, record it in the comment. EvidenceError message
  distinct from all existing classes, naming the offending token and
  the refusal class (an argparse help early-exit token — the recorded
  producer would exit inside argparse before collecting anything —
  wording must hold for ALL six spellings: the bare forms print help
  and exit 0, `--help=x` is a usage error exiting 2, so the message
  must NOT claim help-printing/exit-0 for the family).
  Comment records the zero-collision premise (no registered `--h*`
  business flag, no single-dash business flag) and that the FULL
  spelling `--help` is rejected too — unlike anchored options, where
  the full spelling is the legitimate binding, every member of the
  help family is non-production.
- [ ] 2. argv[0] comment expansion (:1098-1100): keep the three
  existing sentences byte-identical; append the capability consequence
  — argv[0] (the interpreter) plus the repo checkout behind argv[1]
  remain the residual trust roots of the forensic claim, a plan may
  record any interpreter there — and the closure route: producer-side
  hardening (#1261 alternative 2), explicitly NOT a verifier gate
  (pinning argv[0] would pin an environment fact). No code change for
  this shape.
- [ ] 3. Relational `--evidence-dir` gate:
  (a) sixth gate immediately after the tool-value loop (:1135-1141),
  before the token scan: `expected_evidence_dir =
  output_path.rsplit("/", 1)[0] + "/capture-artifacts"` (the
  `output_path` local already validated absolute at :1086-1092;
  string rsplit, NO Path normalization — comment records this is the
  exact textual inverse of plan_author.py:219/:239's same-root
  f-strings, so every plan_author-authored plan satisfies it and no
  run-varying literal is pinned), then
  `_argv_option_values(capture_argv, "--evidence-dir") ==
  [expected_evidence_dir]` else EvidenceError naming the option, the
  observed binding list and the derived expected value (one equality
  refuses absent/duplicated/dangling/mismatched — same shape as the
  #1262 gate; message wording must state the RELATIONAL claim: bound
  to the capture's own output directory, not to a committed literal);
  (b) `PINNED_CAPTURE_VALUE_OPTIONS` (:142) gains `"--evidence-dir"`
  (dynamic like `--database` — NOT added to
  EXPECTED_CAPTURE_TOOL_VALUES; the whole-dict literal pin and drift
  guard stay untouched); extend the :128-141 comment with the
  measured `--e*` zero-collision premise (`--evidence-dir` is the only
  registered `--e*` flag; `--e` at len 3 falls inside the mechanism);
  (c) rewrite the :113-118 deliberately-absent rationale:
  `--evidence-dir` moves OUT of the absent list — record its
  statvfs-measurement-input identity (capture.py `_free_bytes`
  :490-502 measures ctx.evidence_dir :501; snapshot free_bytes :472
  feeds the MIN_FREE_BYTES hard gates :2046-2047/:2201-2203; #1250
  closed the seam route, this gate closes the directory-identity
  route) and that the binding is relational; the `--schema-dump-*`
  sentence stays.
- [ ] 4. Template extension
  (`tests/test_node27_timeseries_compression_live_evidence.py`) —
  single-point change per template, attribution-preserving:
  (a) `_bundle` capture template (:1172-1188) gains
  `"--evidence-dir", str(tmp_path / "capture-artifacts")` (every
  bundle capture output_path lives directly under tmp_path — the
  derived value is uniform across kinds; derive it from tmp_path, do
  NOT hardcode a string); the template comprehension has tmp_path in
  scope already;
  (b) `_producer_argv` (:5350-5368) grows a required
  `evidence_dir: str` parameter (or equivalent explicit argument —
  implementer's choice, but it MUST be caller-supplied per test since
  the expected value is tmp-dependent; a module-level constant cannot
  work) baked into the returned argv; `--mutation-head-sha` STAYS
  `*extra` (the `[pair_missing]` red capability from #1261 must
  survive — recorded reason in the docstring stays); every existing
  call site updates mechanically to pass
  `str(tmp_path / "capture-artifacts")`, test BODIES otherwise
  untouched;
  (c) `_pinned_capture_options` docstring updated if its "production
  values throughout" claim would otherwise go stale (evidence-dir is
  tmp-derived, not production — either exclude it from that helper and
  bind it in the templates, or amend the docstring; do not let a
  stale claim ship).
- [ ] 5. New tests (all reusing `_replace_capture_argv` /
  `_producer_argv` + `_CAPTURE_EQUALITY_ERROR not in message`
  non-vacuity discipline):
  (a) help-token rejection, parametrized over the six spellings
  `-h`, `--help`, `--help=x`, `--h`, `--he`, `--hel` — each appended
  as a trailing token to an otherwise fully valid argv (anchored +
  pinned + evidence-dir all correct), each refuses with the offending
  token in the message and no seam/anchored/pinned wording;
  (b) help-token position independence: one case with the token
  mid-argv (e.g. between pinned pairs) refuses identically;
  (c) structural premise: parser introspection over
  `_capture._parser()._actions` — no registered option string other
  than `--help` starts with `--h`, and the only single-dash option
  string is `-h` (pins the zero-collision premise task 1's comment
  records; same pattern as the existing #1250/#1259/#1261 structural
  tests);
  (d) `--evidence-dir` four-shape matrix: mismatched (a sibling
  directory under tmp_path that exists but is NOT the output_path
  sibling), absent, duplicated pair (`--evidence-dir X` appended
  twice), dangling inline (`--evidence-dir=`) — each refuses with
  `--evidence-dir` and the derived expected value in the message;
  (e) abbreviation rebind: `--ev <other>` and `--e <other>` appended
  to an otherwise-valid argv each refuse via the pinned-prefix branch
  (message: pinned capture tooling value wording);
  (f) relational-not-absolute positive: a bundle whose capture
  output_path is moved to a tmp SUBDIRECTORY (via
  `_replace_produced_artifact`-style rewrite or a direct plan edit)
  with `--evidence-dir` rebound to THAT directory's
  `/capture-artifacts` sibling still passes the sixth gate (fails
  later or verifies — assert specifically that the refusal, if any,
  is NOT the evidence-dir gate; this proves the gate is relational,
  not pinned to tmp_path);
  (g) structural: EXTEND the existing zero-collision test
  `test_capture_cli_has_no_flag_abbreviating_a_pinned_capture_option`
  (:5753-5774) — cardinality literal 8 → 9 (the only permitted body
  change, see Must preserve) plus a new assertion that
  `--evidence-dir` is the only registered `--e*` capture flag; do
  NOT create a separate accompanying test while leaving the
  cardinality assertion red;
  (h) existing twelve-kind plan_author positive control and the e2e
  run unmodified and green (they are the production-relation and
  hermetic-relation positive controls — no new positive needed
  there, but their green-ness is part of this change's evidence).
- [ ] 6. Full suites + red proofs (orchestrator Phase 2 reproduces):
  `uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_supervisor.py` all green
  (baselines 338/14/141 + new tests; frozen suites' counts
  unchanged); red proof by hunk-level `git apply -R` of the THREE
  load-bearing hunks — help branch, sixth gate, AND the :142 tuple
  extension (the 5(e) `--ev`/`--e` negatives are carried by the
  tuple element through the UNCHANGED pinned-prefix loop, so a
  two-hunk revert would leave them red-capable and the "every new
  negative DID NOT RAISE" claim false): with all three reverted,
  every new negative goes DID NOT RAISE (record the per-test-id
  list) while the pre-existing suite stays green EXCEPT exactly one
  expected red —
  `test_capture_cli_has_no_flag_abbreviating_a_pinned_capture_option`
  fails its cardinality pin by AssertionError (asserts 9, reverted
  tuple is 8); that red IS the :142 hunk's own attribution proof and
  is recorded separately from the DID-NOT-RAISE list; any other
  pre-existing red fails the proof — proving the new
  tests are load-bearing on exactly the new code;
  `uv run ruff check .`; `openspec validate capture-argv-residual-shapes
  --strict --no-interactive`; frozen-surface zero-diff check
  (`git diff --stat` names only the two permitted files).

## Evidence Floor

- Three suites green with counts recorded (baseline 338/14/141; only
  the live_evidence count grows).
- Red proof: hunk-revert of the THREE load-bearing hunks (help
  branch + sixth gate + :142 tuple element) → all new negatives DID
  NOT RAISE with the per-test-id list recorded; pre-existing tests
  green except exactly one expected AssertionError red — the
  zero-collision test's cardinality pin (9 vs reverted 8), the :142
  hunk's own attribution proof, listed separately; any other
  pre-existing red fails the floor (attribution intact both
  directions).
- Frozen surfaces zero diff; #1250/#1259/#1262 message-substring
  assertions green unmodified.
- `uv run ruff check .` clean; openspec strict validation green.
- PR body records: the six help spellings and their executed-proof
  provenance (issue #1263), the relational derivation and its
  plan_author inverse, the recorded argv[0] residual-trust-root
  closure route, and any deviation from this fixture (`偏离记录`,
  explicit "no deviations" otherwise).
