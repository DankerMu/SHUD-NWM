# Tasks: capture-tool-path-value-pin

Fixture level: expanded · Repair intensity: standard · Issue #1261

Review record: fixture review round 0 → REVISE (2 P1 + 2 P2 +
1 note, precise folds); repair iteration 1 folded F1
(_producer_argv template must extend with _bundle — a #1259
positive test would otherwise redden against a Must-preserve that
declared it green, inviting a present-only gate downgrade), F2
(e2e cleanup --repo exception: per-kind exec-side rewrite, no kind
exemption in the gate), F3 (whole-dict literal pin +
inspect.signature drift-guard mechanics), F4 (executed-argv stub
fidelity pins pre-rewrite), F5 (4(f) concrete twelve-argv form,
gate-by-gate fallback deleted as tautological), Note (abbreviation
message wording split anchored vs pinned).

Triage note: M — one code surface (verifier) + one test file, fully
local hermetic; the decisive complexity is the e2e restructure (the
issue body missed it, explorer caught it pre-fixture: plan_prod
feeds STUB tool paths to the real verify_bundle today, so the pin
breaks the e2e unless the tool-value divergence moves to plan_exec,
mirroring the #1259 argv[1] split). Second decisive point applied
from PR #1260 round 1: every newly pinned option enters the
proper-prefix rejection domain in the SAME change — an exactly-once
full-name check without abbreviation closure is the exact bypass
class that was verifier-CONFIRMED there; we do not re-discover it in
review. Risk axes: (1) TOOL-VALUE REJECTION — stub/mismatched/
missing/duplicated bindings for the pinned set are EvidenceError
before any PASS; (2) ABBREV CLOSURE — `--ps`-class rebinds fall in
the rejection domain; (3) HERMETIC COMPATIBILITY — the e2e still
executes stub tools via plan_exec while plan_prod records production
values and still verifies PASS; (4) FROZEN SURFACES — capture.py,
plan_author.py, supervisor.py, bundle_author.py, schemas/**, the
capture and supervisor test files: zero diff; #1250/#1259 gates and
messages byte-stable.

Line anchors (explorer-verified at master 3851185f):
live_evidence.py — EXPECTED_REPO_PATH :84, ANCHORED_CAPTURE_OPTIONS
:98, _argv_option_values :623-647, expected_executable :651-661
(function :650-777), plan database check :981, plan repo_path check
:982, captures loop gates :1050-1113 (concrete :1050, identity
:1057-1062, kind positional :1063-1067, kind exactly-once
:1072-1076, sha exactly-once :1077-1081, per-token scan
:1102-1113). plan_author.py — DEFAULT_REPO :36, DEFAULT_CONTAINER
:47 (= supervisor.EXPECTED_CONTAINER = "nhms-db"), DEFAULT_DATABASE
:48 (= "nhms"), tool defaults :99-103 (/usr/bin/psql, systemctl,
docker, journalctl, git), capture_common :214-225 (option order:
--database --mutation-head-sha --repo --container --evidence-dir
--psql --systemctl --docker --journalctl --git), schema_dump extras
:229-233 (defaults :38-39), CLI flags :290-307 (no --capture-repo
flag — capture_repo is kwarg-only), runbook invocation passes only
--mutation-head-sha/--output (tier-node27 :1034-1038).
capture.py — REPO_PATH :48, HOST_DOCKER_CLI :54 (= /usr/bin/docker),
docker half-guard :511-513 (schema_dump_list kind ONLY),
_container_state docker call :274 unguarded, parser :755-768 (all
eight tool/path flags required=True, no path validation).
tests/…live_evidence.py — _bundle capture argv :1154-1161 (NO tool
options today), e2e :4908 (capture_bin :4927, build_run_plan call
:4989-5001 with stub capture_psql/…/capture_git + capture_repo,
plan_exec deepcopy + argv[1]-only capture swap :5018-5043,
run-plan.json serialize :5108, verify_bundle+PASS :5121-5123).
Blast radius: prearm plan_owned_paths reads output_path/capture_id/
kind only, never argv — unaffected; no other argv construction site
exists. Schema: run_plan is an opaque artifact_ref — zero schema
change.

Must preserve:
- `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_plan_author.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `scripts/node27_timeseries_compression_supervisor.py`,
  `schemas/**`, `tests/test_node27_timeseries_compression_capture.py`,
  `tests/test_node27_timeseries_compression_supervisor.py`: zero
  diff.
- #1250 seam scan and #1259 identity/anchored gates: behavior and
  messages byte-stable; the two argv template helpers (`_bundle`
  :1154-1161 AND `_producer_argv` :5260-5263) each change at one
  point, every TEST BODY stays zero-diff, and all #1250/#1259
  assertions stay green — explicitly including
  `test_verifier_accepts_the_inline_mutation_sha_form_when_it_matches`
  (:5341-5353), which after the template extension doubles as the
  new gate's non-vacuity positive control, and
  `test_verifier_rejects_capture_argv_without_the_plan_mutation_sha`
  (all four param ids, :5315-5338 — the `_producer_argv` SHA
  carve-out in task 4(a) exists precisely to keep `[pair_missing]`
  red-capable).
- The pinned set applies to ALL twelve capture kinds uniformly —
  no per-kind exemption exists in the gate (the e2e cleanup --repo
  special case lives entirely on the EXEC side, task 3).
- The e2e still asserts PASS_VERDICT + qualifies_task_4_5 + all
  #1250 fidelity pins PLUS the new executed-argv stub pins (task
  3); assertion count never decreases.
- `_bundle()`/`_producer_argv` consumers: only the argv
  construction inside the two helpers changes; every consuming
  assertion stays untouched and green.
- Command-side `_validate_exact_command_argv` untouched.

## Implementation tasks

- [x] 1. Verifier tool-value gate —
  `scripts/node27_timeseries_compression_live_evidence.py`:
  (a) module constant `EXPECTED_CAPTURE_TOOL_VALUES` mapping
  `--psql → "/usr/bin/psql"`, `--systemctl → "/usr/bin/systemctl"`,
  `--docker → "/usr/bin/docker"`,
  `--journalctl → "/usr/bin/journalctl"`, `--git → "/usr/bin/git"`,
  `--repo → EXPECTED_REPO_PATH`, `--container → "nhms-db"` —
  restated literals with a comment recording the independent-oracle
  posture (NOT imported from plan_author/supervisor) and that a
  drift-guard test (task 4(e)(ii)) binds them to plan_author
  defaults;
  (b) fifth gate in the captures loop, after the mutation-sha gate
  (:1077-1081): for each `(option, expected)` in the map,
  `_argv_option_values(capture_argv, option) == [expected]` else
  EvidenceError naming the option, the observed binding list and
  the expected value (absent → `[]`, duplicate → two entries,
  dangling → `[""]`, mismatch → wrong value: one check refuses all
  four shapes — the message must make the observed list visible);
  (c) same gate, dynamic entry: `_argv_option_values(capture_argv,
  "--database") == [<plan database>]` reusing the exact variable
  the :981 plan-level check already validates (do not re-derive).
- [x] 2. Abbreviation closure — widen the existing per-token
  proper-prefix rejection (:1102-1113) so the option set it guards
  is `ANCHORED_CAPTURE_OPTIONS + tuple(EXPECTED_CAPTURE_TOOL_VALUES)
  + ("--database",)` (mechanism unchanged: reject when
  `len(base) >= 3 and base != option and option.startswith(base)`,
  `base = token.split("=", 1)[0]`). Keep the seam-scan branch
  byte-identical, and keep the EXISTING abbreviation message for
  the two anchored options exactly as #1259 shipped it (its tests
  assert the `abbreviation of --kind` / `abbreviation of
  --mutation-head-sha` substrings); the newly pinned options get
  their own message sentence (naming the option and the token) —
  do NOT reuse the "anchored capture identity" wording for tool
  options (fixture-review note: semantically wrong there). Comment
  records: zero collision measured (no registered capture flag is
  a proper prefix of a pinned option; no pinned option is a proper
  prefix of another pinned option; plan_author emits full flags
  only; seam tokens are prefixes of nothing pinned), and that
  ambiguous bases (`--d`, `--c`…) are rejected even though
  argparse would refuse them as ambiguous — strictly safe. `--s`
  keeps hitting the seam branch first (#1250 message tests
  unaffected).
- [x] 3. e2e restructure —
  `tests/test_node27_timeseries_compression_live_evidence.py` e2e
  (:4908): plan_prod's `build_run_plan` call drops
  `capture_psql/capture_systemctl/capture_docker/capture_journalctl/
  capture_git` and `capture_repo` (captures now carry production
  defaults; `schema_dump_host/container` overrides STAY — those
  options are unpinned; `capture_python=sys.executable` stays —
  argv[0] unpinned). DELETE the existing plan_prod cleanup `--repo`
  rewrite block (:5002-5010) — that exception moves wholesale to
  the exec side (fixture-review F2): the plan_exec divergence loop
  rewrites, per capture argv, the five tool option values to the
  `capture_bin` stub paths and the `--repo` value PER KIND —
  `cleanup` → `str(ROOT)` (its verifier checks repo_units service
  paths against the canonical checkout, live_evidence.py:591-598 +
  :2806-2825, and capture.py:576-578 reads units from ctx.repo),
  every other kind → `str(fixture_repo)`. Position-independent
  rewrite by option name (small local helper; do NOT assume
  capture_common's fixed offsets). The existing ledger rewrite
  maps executed argv back to plan_prod argv by capture_id — reuse,
  no new machinery. NEW fidelity pins (fixture-review F4): before
  the ledger rewrite (:5095-5103), assert for every executed
  capture argv (reuse the existing `executed_capture_argv` dict
  :5075-5077) that the five tool options bind to the stub paths
  and `--repo` binds to the checkout path (`str(ROOT)` for
  cleanup, `str(fixture_repo)` otherwise) — without these, a no-op
  rewrite silently executes the REAL host binaries
  (/usr/bin/systemctl etc. exist on the runner) instead of
  erroring. Stop clauses: (i) if an executed capture fails because
  a stub tool is not invoked, fix the rewrite, never relax the
  gate; (ii) if `verify_bundle` fails on
  `cleanup.repo_units.* path is not the canonical checkout path`,
  the cleanup exec-side `--repo` went to the wrong value — fix the
  per-kind rewrite, and NEVER add a per-kind `--repo` exemption to
  the gate. The #1250 fidelity pins and PASS assertion must
  survive unmodified.
- [x] 4. Test fixture + new tests
  (`tests/test_node27_timeseries_compression_live_evidence.py`):
  (a) BOTH argv template helpers extend, but NOT identically
  (fixture-review F1, corrected in repair iteration 2):
  - `_bundle()` (:1154-1161) keeps its baked
    `"--mutation-head-sha", HEAD` and gains `--database` (the
    database the bundle already uses), `"--repo"`
    evidence.EXPECTED_REPO_PATH, `"--container" "nhms-db"`, and
    the five `/usr/bin/*` tool options;
  - `_producer_argv` (:5260-5263) gains ONLY
    `--database`/`--repo`/`--container` + the five tool options;
    `--mutation-head-sha` STAYS caller-supplied via `*extra` —
    recorded reason: the `[pair_missing]` param (:5318) needs a
    template with NO SHA binding; baking it in would turn that
    negative into a fully valid argv (DID NOT RAISE) and silently
    delete the "producer invoked without any SHA pair" coverage.
  Values via the verifier map where a public constant exists;
  one-point change per helper, every consuming TEST BODY
  untouched — in particular
  `test_verifier_accepts_the_inline_mutation_sha_form_when_it_matches`
  (:5341-5353) AND
  `test_verifier_rejects_capture_argv_without_the_plan_mutation_sha`
  (all four ids, :5315-5338) must pass UNMODIFIED; the inline-form
  test doubles as the new gate's non-vacuity control (forbidding a
  present-only downgrade). Keep the new gate AFTER the sha gate
  (task 1 order) so `_producer_argv`-based sha-rejection tests
  keep hitting their original messages;
  (b) parametrized rejection tests over the pinned map: for each
  option — value mismatch (stub path), option absent, option
  duplicated (second binding appended, both spellings), each
  refusing with the option named in the message and
  `_CAPTURE_EQUALITY_ERROR not in message` (same non-vacuity
  discipline as the #1259 tests, reusing `_replace_capture_argv`);
  (c) `--database` mismatch rejection; (d) abbreviation-rebind
  rejection: `--ps <stub>`, `--do <stub>`, `--rep=<path>` appended
  to an otherwise-valid argv each refuse; (e) structural tests:
  (i) zero-collision — no registered capture-CLI flag other than
  the option itself is a proper prefix (len >= 3) of any pinned
  option (parser introspection, same pattern as the existing
  #1259/#1250 structural tests); (ii) drift guard —
  `evidence.EXPECTED_CAPTURE_TOOL_VALUES` values equal the
  plan_author defaults field-by-field; the five tool defaults are
  FUNCTION SIGNATURE defaults, not module constants
  (fixture-review F3) — read them via
  `inspect.signature(plan_author.build_run_plan).parameters["capture_psql"].default`
  etc.; `--repo` ↔ `plan_author.DEFAULT_REPO`, `--container` ↔
  `plan_author.DEFAULT_CONTAINER`; (iii) key-set/literal anchor
  (anti-tautology, same pattern as the #1259 literal pin :5401):
  `assert evidence.EXPECTED_CAPTURE_TOOL_VALUES == {"--psql":
  "/usr/bin/psql", "--systemctl": "/usr/bin/systemctl",
  "--docker": "/usr/bin/docker", "--journalctl":
  "/usr/bin/journalctl", "--git": "/usr/bin/git", "--repo":
  "/home/nwm/NWM", "--container": "nhms-db"}` — whole-dict literal
  comparison, so a two-entry map cannot pass the parametrized
  suites vacuously;
  (f) positive control (fixture-review F5, concrete form — the
  gate-by-gate alternative is deleted as tautological): build a
  `_bundle(tmp_path)`, then for each of the twelve kinds use
  `_replace_capture_argv` to swap in the corresponding capture
  argv from `plan_author.build_run_plan(mutation_head_sha=HEAD,
  root=str(tmp_path))`, and assert
  `verify_bundle(...)["verdict"] == PASS_VERDICT` — preconditions
  all hold (capture_python defaults to sys.executable,
  DEFAULT_CAPTURE_SCRIPT == EXPECTED_CAPTURE_SCRIPT is already
  pinned :5401-5410, DEFAULT_DATABASE equals the `_bundle`
  database, --evidence-dir/--schema-dump-* are unpinned).
- [x] 5. Red proof — one stash cycle: stash/revert ONLY the
  live_evidence.py hunk → the new rejection tests (4b-d) fail
  (record signatures — bundles verify PASS or wrong error, i.e.
  DID NOT RAISE shapes), structural/drift tests that reference the
  new constant fail by AttributeError (record as such — they are
  post-fix pins, mirror the #1260 red-proof note), all #1250/#1259
  tests and the restructured e2e stay green; restore and record
  commands verbatim.
- [x] 6. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_supervisor.py` all green
  (record before/after counts; capture AND supervisor files
  unchanged counts); `uv run ruff check .`; `openspec validate
  capture-tool-path-value-pin --strict --no-interactive`;
  `git diff --stat` → exactly live_evidence.py + its test file
  (+ this fixture); zero diff vs master for every frozen surface
  listed in Must preserve.
