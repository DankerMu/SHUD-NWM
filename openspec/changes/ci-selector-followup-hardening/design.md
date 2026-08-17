# Design: ci-selector-followup-hardening

## Change surface

`scripts/select_ci_tests.py` @ master c843cf23 (584 lines):
changed-test branch `select_tests` :450-467 (condition `tests/**.py`
:450, `CHANGED_TEST_FILE_RULES` loop :452-459, self-select fallback
:461, unconditional meta-guard accumulation :465-466 gated on
`CHANGED_TEST_META_GUARD_PATTERN = "tests/test_*.py"` :19);
`PATH_TEST_RULES` (48 entries, one deliberate duplicate pattern
`services/orchestrator/scheduler.py` — index ~3 narrow non-stop +
index ~11 `stop_on_match=True`); missing-target filter :492-509 (runs
AFTER accumulation — deleted targets are dropped with a warning, the
meta-guard survives); `_write_github_output` :553-557 (count/tests/
tests_json); CLI `main` :560-585.
`.github/workflows/ci.yml`: `unit-test-targeted` :215-268 — two-branch
run step (`count != "0"` → `check=True` targeted; else labeled
collect-only smoke with `::warning` + step summary + redirect-not-pipe
log handling).
`tests/test_select_ci_tests.py` (926 lines): `GATING_MARKER_NAMES`
:578 frozen `{integration, e2e}` with prose comment :569-577 naming
conftest :74-88 as the real skip source; `GUARDED_MODULE_CLOSURES`
:584 (2 modules, anti-vacuity member); `_tracked_top_level_test_files`
:600 (`fnmatch tests/test_*.py` — nested dirs invisible, zero tracked
instances); `_file_level_gating_markers` :604 (AST pytestmark);
`_non_gated_top_level_importer_tests` :636 (direct importers only —
contrast `_contract_dependent_test_closure` :443 which is fixed-point);
closure guard test :649; function-body-importer exclusion pin
:692-700 (`test_analysis_pipeline.py` / `test_gateway_reconcile.py`
deliberately NOT selected for direct function-body imports);
changed-test meta-guard pins :738-786
(incl. the #1453-defect pin :761-767 `select_tests([conftest]) ==
[conftest]` — the ONLY assertion pinning support-file selection);
route-C pins block :812-838 (comment says "All seven are
pins, NOT endorsements"); github-output pin :881.
`tests/conftest.py` :62-88 (read-only anchor): registers markers incl.
`real_disk` :65, `timescaledb_210` :67-71;
`pytest_collection_modifyitems` :74-88 auto-skips exactly
`integration`/`e2e`/`grib` via `"<marker>" in item.keywords`
membership tests (:83-88) — the AST anchor must target that shape,
not `get_closest_marker`.

Verified facts (issue claims re-derived by fixture review at HEAD —
the issue's "64" fixed-point figure was FALSIFIED and is not used):
support-file self-selection + pytest exit 5 reproduced on all **8**
tracked non-`test_*` `tests/**.py` files (`conftest.py`,
`integration_helpers.py`, `__init__.py`, `mock_shud_omp.py`,
`river_identity_backfill_fakes.py`, `slurm_template_helpers.py`,
`fixtures/mapping_builder/in_memory_grid_snapshot.py`,
`fixtures/mapping_builder/keliya/build.py`); deleted-test-file
selection went 0→1 across #1452; `real_backend` importer geometry:
3 non-test top-level importer modules
(`services/orchestrator/reconcile.py:45`,
`services/production_closure/slurm_validation.py:62`,
`services/slurm_gateway/mock_backend.py:28`; `gateway.py:137` is a
function-body import and stays out), one-hop required set = 14 suites
(5 direct + 9 hop-contributed), 8 of them unselected today
(anti-vacuity member `tests/test_reconcile_sacct_parse.py`;
`tests/test_gateway_reconcile.py` already selected); under the same
top-level-edge definition the unbounded fixed point EQUALS the
one-hop set today (nothing top-level-imports the hop modules; the
only further importer is function-body in
`services/orchestrator/scheduler_runtime.py`), while an
any-depth-edge derivation reaches 71 suites — that is the blowup the
hop bound forecloses; measured cost of running the 8: 754 passed /
~20 s locally; `PATH_TEST_RULES` today: 48 entries / 47 unique
patterns (deliberate `services/orchestrator/scheduler.py` pair at
indices 3 and 11); helper importer counts (for the rejected mapping
option): `integration_helpers` 13, `slurm_template_helpers` 2,
`river_identity_backfill_fakes` 2, `mock_shud_omp` 0.

Risk triage: compact fixture. Pure CI-gate tooling — no runtime code
path, no DB, no display surface; blast radius is which tests PRs run.
Highest risks: (1) selection-semantics regressions on the existing 48
PATH rules and the redirect family (the suite pins them densely —
every change must keep the untouched pins green unmodified); (2) rule
growth for #1455 accidentally pulling gated/e2e suites into the PR
lane (constant skips + zero assertions — the exact failure #1447
excluded); (3) ci.yml edits are not executable locally — the yml
branch logic must be simple enough to verify by reading and by
shell-level simulation of the branch conditions; (4) the one-hop
derivation being wrong in either direction (missing the 8, or
exploding toward the 64-suite fixed point).

## Key decisions

1. **#1453 fix lives in the selector, not ci.yml** (issue's
   recommended route): in the changed-test branch, a `tests/` Python
   path whose BASENAME matches neither `test_*.py` nor `*_test.py`
   (round-1 R1.1 corrected the predicate from path-shaped to
   basename-shaped; the retro's invariant-closure pass completed it
   to BOTH of pytest's default `python_files` patterns and anchored
   it against pytest itself — see the anchor test) skips
   self-selection and instead
   `selected.add(SELECTOR_META_GUARD_TEST)`. Rationale: the
   selector's output contract becomes "every emitted file-level target
   is a collectible test file", which ci.yml's `check=True` relies on;
   tolerating exit 5 in ci.yml (alt A) would demote fail-closed to
   warned-green and leave the defect in place. The
   `CHANGED_TEST_FILE_RULES` loop still runs first for such paths
   (today no rule matches a non-`test_*` name; if one ever does, the
   redirect wins — matched_changed_test suppresses the fallback
   either way). The defect pin :761-767 is REWRITTEN to pin the new
   mapping (`== [SELECTOR_META_GUARD_TEST]`), keeping its original
   non-spill intent via the unchanged accumulation-pattern check; a
   NEW tree-derived invariant test enumerates
   `_tracked_python_files("tests")` minus basename-`test_*.py` matches
   (today exactly the 8 files listed under Verified facts) and
   asserts each selects exactly `[SELECTOR_META_GUARD_TEST]` —
   future support modules are covered without name hardcoding, and
   the derivation must be non-empty (anti-vacuity).
   Recorded trade-off (fixture-review P2-2): this converts a
   fail-closed (misleading, zero-information) red into a green whose
   evidence is the meta-guard suite + the #1454 collect-only smoke.
   `conftest.py`/`integration_helpers.py` keep `real-db-integration`
   compensation via the `database` filter (ci.yml :57-58, non-draft);
   the other six support files have no compensating job — accepted:
   the red they lose carried zero assertions too, and the smoke at
   least guards the import surface. The issue's optional
   helper→importer mapping is REJECTED for now (static per-helper
   rule snapshots reintroduce the rot this guard family targets;
   revisit on first real escape), recorded in proposal non-goals.
2. **#1454 output field, not a second selector mode**:
   `_write_github_output` writes
   `meta_guard_only=true|false`, true iff
   `tests == [SELECTOR_META_GUARD_TEST]` (exactly one target and it is
   the meta-guard suite). This is deliberately a POST-filter property:
   a deleted `tests/test_*.py` self-selects, gets dropped by the
   missing-target filter, and leaves `[meta-guard]` — collapsing to
   true. Support-file-only PRs (#1453's new mapping) produce the same
   shape and the same true. HONEST semantics (fixture-review P2-3):
   the flag describes selection SHAPE, not evidence provenance — it
   also fires for selector-development PRs whose diff-specific target
   IS the meta-guard suite (`scripts/select_ci_tests.py` via the
   same-name derivation, and `tests/test_select_ci_tests.py` itself,
   pinned at :749-754). Accepted rather than special-cased: the extra
   cost is one collection pass on exactly the PR class that changes
   the gate, and shape-equality keeps the flag a two-line predicate.
   Consequence for ci.yml wording (decision 3): on this branch the
   smoke labeling must NOT claim "0 assertions were executed" — the
   targeted run did execute; the adapted wording says the selection
   collapsed to the selector meta-guard and the full-tree collect-only
   ran in addition. Any second real target flips the flag false. The
   stdout test list is unchanged (local `pytest -q $(...)` consumers
   unaffected).
3. **ci.yml: additive third condition, no branch restructure**: the
   `count != 0` targeted branch additionally runs the collect-only
   smoke when `steps.targeted.outputs.meta_guard_only == 'true'`,
   AFTER the targeted pytest, reusing the existing labeled pattern
   (`::warning` + step summary adapted to say the selection collapsed
   to the selector meta-guard; redirect-not-pipe with
   `collect-only.log`; collection failure exits 1). The `count == 0`
   branch is byte-identical. Verification is by reading + local shell
   simulation of the condition (no Actions runner locally); a pin in
   the selector suite asserts the literal string `meta_guard_only` is
   consumed by ci.yml's targeted job (cheap string-coupling guard so
   the field cannot silently die on either side).
4. **#1455(1) one-hop, hop-bounded as policy**: extend the
   guard derivation with
   `_one_hop_importer_modules(module)` — tracked non-test `.py` files
   (domain: every tracked `.py` outside `tests/**`, pinned here so
   the derivation is deterministic) that import `module` at top level
   (reusing the existing top-level AST walk), each contributing
   `_non_gated_top_level_importer_tests(its dotted name)`. Exactly
   one hop: the helper is NOT recursive. The bound is FORWARD-LOOKING
   (fixture-review P2-1 corrected the issue's numbers): at HEAD the
   top-level-edge fixed point equals the one-hop set (14 required
   suites, 8 unselected), so the bound costs nothing today; it exists
   to foreclose the any-depth-style blowup (measured 71 suites) if
   deeper import chains appear. Guard asserts the owning rule covers
   direct ∪ one-hop sets. Rules grow accordingly (implementer
   re-derives mechanically and pastes the derivation command + output
   into the PR body as evidence; NO integer from this design is an
   assertion — the spec scenario names only
   `tests/test_reconcile_sacct_parse.py` as the anti-vacuity member).
   Coherence with the function-body exclusion pin :692-700 (fixture-
   review Note 3): that pin excludes DIRECT importers whose import is
   function-body; one-hop operates exclusively on top-level edges at
   BOTH levels (module→module and test→module), so the two rules
   compose without contradiction — a function-body edge never
   contributes at either level, and the pin stays green unmodified.
5. **#1455(2) — SPLIT OUT** (fixture-review P2-4): the disposition
   surface re-derived at HEAD is 73 distinct gap suites across the 9
   directory rules — a triage workload that would dominate this PR
   and put the orchestrator-family runtime budget at risk. It moves
   to a dedicated follow-up change/PR (per issue #1455's own
   two-batch recommendation), which will also own the exclusion-table
   design — including the P2-6 correction that a `gated` reason
   token must mean gating INVISIBLE to `_file_level_gating_markers`
   (function-level markers, opt-in env gates), since file-level-gated
   suites never derive as gaps and would make such entries stale by
   construction.
6. **#1455(3) duplicate-pattern guard = allowlist form (b)**:
   `INTENTIONAL_DUPLICATE_PATTERNS = frozenset({"services/orchestrator/scheduler.py"})`
   in the test suite next to the guard; the guard asserts every
   pattern appearing >1× in `PATH_TEST_RULES` is allowlisted, and the
   allowlist has no stale members (every member IS duplicated —
   anti-rot in both directions). Forms (a)/(c) rejected: adjacency or
   stop-ordering constraints encode incidental structure, while the
   allowlist directly expresses "this duplication is a decision".
   `CHANGED_TEST_FILE_RULES` is out of the guard's domain (its
   duplicates + `only_when_any_changed` are the #1254 design). The
   #1443 collision is pinned by simulation: a test constructs the
   would-be merged rule list (today's rules + a second
   `packages/common/display_coverage.py` entry) and asserts the guard
   function flags it — so the day #1443 lands, the suite goes red at
   the guard with a message pointing at consolidation, instead of
   silently splitting ownership.
7. **#1455(4) marker anchor is a single equality**: derive the
   auto-skip marker set from `tests/conftest.py`'s
   `pytest_collection_modifyitems` AST — the code shape is
   `"<marker>" in item.keywords` membership tests (conftest :83-88),
   NOT `get_closest_marker`; the extraction targets that shape and
   fails loudly (assert non-empty) on shape drift rather than
   returning empty. The binding assertion is the EQUALITY
   `derived == GATING_MARKER_NAMES | {"grib"}` (fixture-review P2-5:
   the earlier "difference == {grib}, subset implied" framing is
   wrong — if conftest stops skipping `e2e`, the difference test
   still passes while the subset is violated; only the equality reds
   on every add OR remove). `"grib"`'s recorded absence carries its
   rationale (zero file-level `pytestmark` users today) in the
   comment; additionally assert
   `{"real_disk", "timescaledb_210"} ∩ derived == ∅`.
   Falsifiability seam (P2-7): the derivation function takes the
   conftest source (path or text) as a parameter so red evidence
   feeds a modified copy in-memory — same pattern for the duplicate
   guard's rule-list parameter; no tracked-file mutation needed.
8. **Deliberate deferrals recorded**: nested `tests/<pkg>/test_*.py`
   invisibility (zero tracked instances; touching
   `CHANGED_TEST_META_GUARD_PATTERN` and `_tracked_top_level_test_files`
   together is a cross-surface widening with no present-day payoff) —
   stays out, noted here per #1455's "可选" framing.
   SUPERSEDED by round-1 finding R1.1: #1453's mapping turned that
   invisibility from a missed-widening into an active
   misclassification (a nested suite would be treated as a support
   module, losing self-selection AND the meta-guard accumulation), so
   the selector's predicate became basename-shaped
   (`CHANGED_TEST_SUITE_BASENAME_PATTERN`) — a one-surface change;
   `_tracked_top_level_test_files` is untouched, it feeds the
   importer-closure domain. Route-A/B
   empty-selection policy stays #1182-family property (#1454's smoke
   revival does not change the `count == 0` branch's gate strength).

## Must preserve

- All existing selector-suite tests green with ZERO modifications
  except the single sanctioned rewrite of the #1453-defect pin
  (:761-767) and comment-block updates (route-C :812-838,
  GATING_MARKER_NAMES :570-578) whose assertions do not weaken.
- Selection semantics for every input class not named by the three
  issues: PATH rules, redirect family, same-name script derivation,
  CORE_SMOKE fallback, missing-target warning, `count == 0` ci.yml
  branch — byte-identical behavior (the suite's existing pins are the
  oracle).
- Newly-added rule targets must all be non-gated (no file-level
  `integration`/`e2e` pytestmark) and existing tracked files.
- `--github-output` existing fields (`count`, `tests`, `tests_json`)
  unchanged; stdout format unchanged.
- ci.yml: only the `unit-test-targeted` "Run targeted tests" step
  changes; job triggers, filters, timeout, and every other job stay
  byte-identical.

## Seams under test

Pure-function seams already used by the suite: `select_tests(paths,
repo_root=...)` with real tree or `tmp_path` roots (deleted-file
scenarios use a tmp root lacking the file, per the :769 pattern);
`_write_github_output` to a tmp file; guard helpers are plain
functions over the tracked tree. The duplicate-pattern guard is
factored as a function over a rule list so the #1443 simulation can
feed a constructed list. No new seams needed; no monkeypatching of
git.

## Test plan (maps to acceptance)

1. #1453: tree-derived support-file invariant (each non-`test_*`
   `tests/**.py` → exactly `[meta-guard]`, derivation non-empty);
   rewritten :761 pin; collectibility of the mapped selection is
   implied by the meta-guard suite being a real test file (no
   per-file pytest-collect subprocess in unit tests).
2. #1454: tmp-root deleted-test scenario → selection `[meta-guard]`,
   github-output `meta_guard_only=true`; multi-target and empty
   selections → `false`; ci.yml string-coupling pin; route-C comment
   updated.
3. #1455(1): guard derives one-hop set for `real_backend` ⊇ the 8
   (anti-vacuity member `test_reconcile_sacct_parse.py`), rule covers
   it; removing a rule entry (simulated on a copied rule list or by
   the guard's own failure message contract) reds the guard.
4. #1455(3): duplicate guard green on today's table; scheduler
   allowlist member asserted actually duplicated; simulated #1443
   merge list → guard flags `packages/common/display_coverage.py`.
5. #1455(4): marker-anchor equality per decision 7, red evidence via
   the conftest-source parameter seam (modified copy in-memory).
6. Runtime accounting (fixture-review P2-4 residue for this PR's
   scope): record selected-set size before/after for the touched
   `services/slurm_gateway` rule and the measured wall-clock of the
   newly-required suites in the PR body.
7. Full: `uv run pytest -q tests/test_select_ci_tests.py`;
   `uv run ruff check .`; CLI spot checks from the issues'
   Verification fields (`printf 'services/slurm_gateway/real_backend.py\n' | uv run python scripts/select_ci_tests.py`
   etc.). Red evidence: each new guard/pin fails against the
   unmodified selector (stash-free: in-memory or out-of-tree copies).

## Risks to watch

- Fail-closed → green conversion for the 6 uncompensated support
  files (decision 1) is a deliberate gate-strength trade recorded
  above — reviewers should challenge it if they find a helper whose
  regression the collect-only smoke cannot catch AND whose importers
  are cheap to select.
- Rule growth for the one-hop set changes what CI runs on
  `services/slurm_gateway` PRs — measured ~20 s / 754 passed for the
  8 new suites, well inside the 35-min cap; re-measure at HEAD and
  record in the PR body.
- `meta_guard_only` computed on the POST-filter list: keep it in
  `_write_github_output`'s caller path (`main`, :560-581) operating
  on the final returned list, not inside `select_tests`.
- All derivation helpers used by guards must take their inputs
  (rule list, conftest source, tree root) as parameters (P2-7) so
  every guard has an in-memory red-evidence seam.
