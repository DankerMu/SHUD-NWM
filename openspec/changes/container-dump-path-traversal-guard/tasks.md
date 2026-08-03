# Tasks — container-dump-path-traversal-guard (#1269)

Risk triage: fixture level **expanded** (issue has no suggested level;
S-size but it edits two forensic gate scripts whose refusal semantics
are the lane's trust boundary, and it corrects a false security claim
— compact under-covers that). Risk packs selected:
**forensic-verbatim-posture** (values must stay unrewritten
end-to-end; #1265/#1268 adjudications must survive) and
**guard-soundness** (each gate refuses independently; refusal precedes
side effects). Not selected: performance (pure predicate, no hot
path), UI/display (lane is backend-only), migration/schema (no
persisted-format change).

Must-preserve behavior:

- The #1268 adjudication pin scenario/test: interior-`//` container
  path authors at plan_author and lands verbatim as the argv tail.
- `test_resolve_container_pg_restore_identity_reads_real_docker_probes`
  (tests/test_node27_timeseries_compression_supervisor.py:2494-2509)
  byte-identical assertions.
- All existing PASS-lane positives (the twelve-kind control, capture
  argv gates, whole-argv exact equality at live_evidence.py:1522).
- Every existing refusal message spelling (no message churn anywhere).

Seams under test (upstream-declared, consumed not renegotiated): the
four gate call sites (live_evidence :744-749 / :1887-1892, supervisor
:350-364 / :1055-1059) and gate 4's refusal-before-probe ordering.
Needed-but-missing seam, found by fixture review (P1) and folded in as
gate 5: the pre-spawn capture-argv value gate in
`_assert_capture_producer_argv` (supervisor :519-551, called :661 /
:964) — recorded here as a deviation from the issue's four-gate
boundary, adopted because without it the ADDED requirement's
"before any container side effect" claim would be false on the
capture-argv route (capture.py:531-535 executes
`docker exec pg_restore --list` on the unchecked value).

Non-goals: plan_author guarding of `schema_dump_container` (#1268
ruling stands); `EXPECTED_CAPTURE_TOOL_VALUES` pin-domain changes; any
normalization of recorded/compared values; #1240/#1255/#1266 same-lane
follow-ups.

Minimal mergeable slice: this whole change is the slice — predicate +
five call sites + tests + spec; splitting any gate out would ship a
knowingly-partial fix ("只改其一等于没改").

## 1. Shared predicate

- [ ] 1.1 Add `CONTAINER_DB_MOUNT_PREFIX` and
  `container_dump_path_within_mount(value: str) -> bool` to
  `packages/common/node27_container_contract.py` (prefix conjunct AND
  `".." not in PurePosixPath(value).parts`; module gains
  `from pathlib import PurePosixPath`). Additive only; no existing
  symbol in the module is touched; no snapshot refresh (the snapshot
  script's contract mapping is explicit per-constant).
- [ ] 1.2 Direct predicate unit tests (in
  `tests/test_node27_timeseries_compression_live_evidence.py`, beside
  the other structural pins): accepts the default
  `/var/lib/postgresql/evidence/schema-before.dump`, interior-`//`
  in-mount value, trailing-slash in-mount value
  `/var/lib/postgresql/evidence/` (parts drops the trailing empty —
  today's gate behavior preserved; a `resolve()`-based
  mis-implementation would flip this row), bare mount root
  `/var/lib/postgresql/` (today's behavior, recorded), and an `a..b`
  filename component; rejects
  `/var/lib/postgresql/../../../etc/shadow`,
  `/var/lib/postgresql/evidence/../../../../etc/passwd`,
  `/var/lib/postgresql/..`, `/var/lib/postgresql/x/..` (the shortest
  escape after a real segment), the empty string (gate 5's
  dangling-flag sentinel), and the prefix miss `/tmp/schema.dump`.

## 2. Four call sites

- [ ] 2.1 Gate 1 (live_evidence.py:744-749): replace the inline
  `startswith` with `container_dump_path_within_mount` imported
  by-name from `packages.common.node27_container_contract` (the
  existing import form at live_evidence.py:49 / supervisor.py:43);
  message `"pg_restore list argv differs"` unchanged.
- [ ] 2.2 Gate 2 (live_evidence.py:1887-1892): same swap on
  `list_argv[-1]`; message `"schema forensic dump/list identity is not
  verifiable"` unchanged.
- [ ] 2.3 Gate 3 (supervisor.py:350-364): same swap in
  `_assert_exact_argv`; message `"pg_restore list argv/output
  ownership differs"` unchanged.
- [ ] 2.4 Gate 4 (supervisor.py:1055-1059): same swap as the
  function's first statement (ahead of every `_run_capture_argv`);
  message `"pg_restore dump path is outside the DB container data
  mount"` unchanged and now truthful.
- [ ] 2.5 Gate 5 (supervisor.py:519-551 `_assert_capture_producer_argv`):
  when `kind == "schema_dump_list"`, every value from
  `_capture_option_values(argv, "--schema-dump-container")` must pass
  the predicate (absent option → empty list → nothing to judge, and
  capture itself fails such a run: `--schema-dump-container` is
  registered `default=None` at capture.py:769 and
  `_capture_schema_dump_list` raises
  `CaptureError("schema_dump_list requires
  --schema-dump-host/--schema-dump-container")` at :515-516 — the
  in-mount default belongs to plan_author, not capture; the
  dangling-flag `""` sentinel fails the predicate naturally); new
  refusal message states the containment claim truthfully. Add
  `"--schema-dump-container"` to `ANCHORED_CAPTURE_OPTIONS` in BOTH
  planes — supervisor.py:76 AND live_evidence.py:98 — because the
  cross-plane equality is pinned
  (tests/test_node27_timeseries_compression_live_evidence.py:5654
  asserts the tuples equal); extend that structural test's recorded
  premise with the analogous `--schema-dump-c*` uniqueness line (its
  per-anchored prefix loop :5650-5653 already passes for the new
  entry), and extend the SAME uniqueness premise in the two tuple
  definitions' own comments (supervisor.py:71-75 and the twin at
  live_evidence.py:92-97), which a third entry otherwise leaves
  incomplete (fixture-review terminal note). The verifier's plan-capture gate (live_evidence.py:
  1259-1264) thereby also newly refuses abbreviation spellings —
  recorded as behavior-change surface, not a side effect. Docstring
  gains the value-check adjudication sentence (execution-safety
  property vs the deliberately unchecked `--mutation-head-sha`
  forensic claim).

## 3. Tests — refusal side

- [ ] 3.1 Per-gate traversal negatives: each of the four gates gets
  its own test(s) covering both traversal shapes
  (`/var/lib/postgresql/../../../etc/shadow`,
  `/var/lib/postgresql/evidence/../../../../etc/passwd`), asserting
  the gate's own exception type and message, reaching the gate
  directly (hand-crafted argv/listing/plan input) — never inferring
  refusal from an upstream gate.
- [ ] 3.2 Strengthen
  `test_resolve_container_pg_restore_identity_rejects_out_of_mount_dump`
  into a parametrized battery: prefix miss `/tmp/schema.dump` plus the
  two traversal shapes.
- [ ] 3.3 Zero-docker-exec proof for gate 4: with `probe_bin`
  installing a docker stub whose empty responses list makes ANY
  invocation surface as a distinguishably different failure (stub
  exits 97 → `_run_capture_argv` raises its label-bearing "checkpoint
  probe" SupervisorError), assert the raise is exactly the
  mount-containment message — proving no `docker exec` ran.
- [ ] 3.4 Gate 5 negatives: direct unit tests of
  `_assert_capture_producer_argv` with `kind="schema_dump_list"`
  covering (a) both traversal shapes in both argparse forms
  (`--schema-dump-container <v>` and `--schema-dump-container=<v>`),
  (b) the abbreviation spelling (`--schema-dump-c=<traversal>` →
  refused as an anchored-option abbreviation), (c) the dangling flag
  (`--schema-dump-container` as final token → `""` sentinel fails the
  predicate), (d) a second late binding after a clean first one
  (last-wins smuggling; the late value is PINNED as the traversal
  path `/var/lib/postgresql/../../../etc/shadow`, so this id is IN
  E4's expected-red set) — each raising `SupervisorError`.
  Refusal-before-spawn is structural (the gate is called at :661/:964
  before any spawn), so no process-level proof is required beyond
  these direct tests.

## 4. Tests — preservation side

- [ ] 4.1 Positive non-regression: existing identity positive
  (:2494-2509) and all five gates' existing positive paths green and
  unmodified (gate 5 positives: the committed capture argv shape with
  a clean `--schema-dump-container`, with the option absent, and for
  the other capture kinds untouched by the new check); add a
  gate-level interior-`//` in-mount positive where one does not
  already exist (gate 4 direct call is sufficient there).
- [ ] 4.2 Verbatim posture: assert admitted values are recorded and
  compared as original strings (existing whole-argv equality tests
  count as coverage; no normalization site appears in the diff).
- [ ] 4.3 Source-scan drift guard: a test asserting the pattern
  `startswith("/var/lib/postgresql/")` appears zero times in
  `scripts/node27_timeseries_compression_live_evidence.py` and
  `scripts/node27_timeseries_compression_supervisor.py` (all five
  sites go through the shared predicate; plan_author's DEFAULT
  constant is a value literal, not a check, and stays out of scope).
- [ ] 4.4 The #1268 adjudication pin test's assertions and pinned
  behavior stay byte-identical and green; its docstring (and the
  plan_author.py:172-186 comment block) get docstring/comment-ONLY
  de-staling — both currently describe the gates as prefix-only,
  which this change makes false. Verify comment-onlyness with a
  non-comment-changed-lines check on plan_author.py.

## 5. Spec + validation

- [ ] 5.1 Spec delta: ADDED requirement (five gates, containment
  predicate, independent refusal, refusal-before-side-effect
  including the pre-spawn capture-argv route, verbatim posture,
  single-source drift guard) + MODIFIED #1268 requirement (residual
  paragraph notes the gates' new `..` refusal without reopening the
  authoring adjudication).
- [ ] 5.2 `openspec validate container-dump-path-traversal-guard
  --strict --no-interactive` green.

## Evidence Floor

- [ ] E1 `uv run pytest -q
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_supervisor.py
  tests/test_node27_timeseries_compression_capture.py` green — the
  capture suite exercises gate 5 end-to-end (its :640-695 rehearsal
  runs `validate_run_plan` → :661 and the producer state machine →
  :964 over a real authored plan) — plus the plan_author suite if any
  shared test module is touched.
- [ ] E2 `uv run ruff check .` green.
- [ ] E3 openspec strict validation green (5.2).
- [ ] E4 **Red proof**: with the predicate's `..` conjunct removed
  (backup-copy mutation of the shared function, never `git checkout`
  on uncommitted work), the expected-red set is exactly: the per-gate
  traversal negatives across all five gates (3.1, 3.4a, and 3.4d
  whose pinned late value is a traversal path — NOT 3.4b/c, which red
  on the abbreviation scan and the prefix conjunct respectively), the
  strengthened :2511 battery's traversal ids (3.2), and the 1.2
  predicate reject rows that carry `..` (`…/etc/shadow`,
  `…/etc/passwd`, `/var/lib/postgresql/..`,
  `/var/lib/postgresql/x/..`) — prefix-miss/empty-string rows stay
  green; nothing else reddens; restore byte-identical and re-run
  green.
- [ ] E5 Surface check (`git diff --stat` + per-file review): only the
  six in-scope code/test files plus openspec change; plan_author.py
  diff proven comment-only (zero non-comment changed lines); frozen
  list untouched.
- [ ] E6 Zero-docker-exec proof (3.3) present and green.
