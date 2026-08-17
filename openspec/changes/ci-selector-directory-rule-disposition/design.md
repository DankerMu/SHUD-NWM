# Design: ci-selector-directory-rule-disposition

## Change surface

`scripts/select_ci_tests.py` @ master d02b4edb: `PATH_TEST_RULES`
(**49** entries after PR #1486: 48 pre-existing + the
`real_backend.py` narrow rule at index 21; stop rules at indices
0-13 cover i.a. `services/orchestrator/{chain_types,chain_manifests,
chain,chain_repository_state,file_orchestration_journal,
file_orchestration_migration,cli,scheduler,scheduler_core,
scheduler_runtime}.py` and
`workers/forcing_producer/direct_grid_contract.py` — later rules are
UNREACHABLE for those paths; several rules hold SHARED module-level
tuples: `FILE_JOURNAL_READ_STATE_TESTS` is also used by
`packages/common/safe_fs.py` outside the nine directories,
`ORCHESTRATOR_MANIFEST_SURFACE_TESTS` by three chain modules —
editing a shared constant leaks selection changes across surfaces);
one allowlisted duplicate pattern `services/orchestrator/scheduler.py`
at two entries. `CHANGED_TEST_FILE_RULES` untouched by this change.
`tests/test_select_ci_tests.py` (1559 lines, 75 tests): derivation
helpers from PR #1486 — `_parse_tracked`, `_top_level_imported_module_names`,
`_file_level_gating_markers`, `_tracked_python_files`,
`_dotted_module_name` (`.__init__`-stripping),
`_non_gated_top_level_importer_tests` (per-module, SLOW — re-parses
all 188 test files per call), `INTENTIONAL_DUPLICATE_PATTERNS`,
duplicate guard, marker anchor, pytest collection anchor.

## Verified facts (orchestrator-derived at d02b4edb, 1.3 s inverted-index run)

Gap map (module→suite pairs where the suite top-level-imports the
module, carries no file-level gating marker, and `select_tests([module])`
does not select it) — CONTEXT for triage, not assertions; the
implementer re-derives at their HEAD:

| directory | modules w/ gaps | pairs | unique suites |
|---|---|---|---|
| workers/output_parser | 3 | 9 | 6 |
| workers/data_adapters | 7 | 25 | 21 |
| workers/forcing_producer | 5 | 31 | 19 |
| workers/shud_runtime | 3 | 8 | 6 |
| workers/model_registry | 11 | 20 | 10 |
| services/orchestrator | 26 | 82 | 45 |
| services/slurm_gateway | 3 | 9 | 8 |
| services/tile_publisher | 2 | 2 | 1 |
| services/production_closure | 17 | 25 | 5 |
| **total** | 77 | **211** | **74** |

Notable structure: package `__init__.py` rows are importers of the
PACKAGE (post-`.__init__`-strip dotted name); heavy repeat offenders
across directories are `test_slurm_array_contract.py` (67 collected),
`test_orchestration_chain.py` (306), and the e2e-named family
(`test_e2e*.py`, `test_ifs_forecast_integration.py`,
`test_two_node_e2e_evidence.py` 844 collected,
`test_two_node_docker_runtime.py` 427, `test_readonly_db_validation.py`
75) — all collect fine (no file-level markers) but are known to carry
function-level/env gating; whether they execute real assertions in
the PR lane is a MEASUREMENT question, not a name heuristic.
`services/production_closure`'s 25 pairs collapse to 5 unique suites
(the `two_node_e2e_*` lane family all points at
`test_two_node_e2e_evidence.py` — the #1452 audit's "单条规则可闭合"
call). Naive per-module derivation measured >2 min for the 9
directories; the inverted-index form (parse each test file once,
invert) measured 1.3 s — the guard MUST use the fast form.

Risk triage: compact fixture. Pure CI-gate tooling; blast radius =
which tests PRs run + selector-suite runtime. Highest risks:
(1) over-selection blowing the 35-min Unit Tests cap for orchestrator
PRs (adding 45 suites to every orchestrator change is the failure
mode — the #1452 audit explicitly routed edge consumers away);
(2) `stop_on_match` ordering: narrow rules added AFTER a stop rule
matching the same path never fire — every addition must be
order-audited; (3) the duplicate-pattern guard from PR #1486:
`services/orchestrator/scheduler.py` already has its allowlisted
pair — its gaps extend an EXISTING entry, never a third; (4) guard
runtime — must stay in single-digit seconds (it rides every
changed-test PR via the meta-guard).

## Key decisions

1. **Disposition is measured, not name-guessed** (F3/F4 sharpened):
   for each ADDITION-CANDIDATE gap suite the implementer runs
   `uv run pytest -q <suite>` once in PR-lane conditions (no opt-in
   env vars; local box ≠ ubuntu runner — caveat recorded) and records
   the 6-tuple (collected, passed, failed, errors, skipped, wall).
   `fn-gated` requires `passed == failed == errors == 0` AND
   `skipped == collected` — a suite that errors or fails is a BROKEN
   gap to surface, never an exclusion. Runs may be time-capped:
   "aborted > N min" rows route to `runtime-budget` with the cap as
   the number. Suites destined for `redirect`/`edge-consumer` need
   no measurement (their evidence is structural, decision 3). The
   measurement table goes into the PR body verbatim.
   Fixture-review preview (5 heaviest names measured): the e2e-named
   family executes real assertions — `test_two_node_e2e_evidence.py`
   844 passed / 137.9 s, `test_readonly_db_validation.py` 75 passed /
   1.4 s — so expect a near-empty fn-gated bucket;
   `test_orchestration_chain.py` was killed at 15 min at 48/306
   (extrapolates > 1 h) and is the canonical runtime-budget shape.
2. **Exclusion table shape**: in the selector test suite,
   `INTENTIONAL_RULE_GAP_EXCLUSIONS: dict[tuple[str, str], str]`
   keyed `(module_path, suite_path)` → reason token, tokens exactly
   {`fn-gated`, `redirect`, `edge-consumer`, `runtime-budget`}.
   Per-pair keying is deliberate (fixture-review axis-2b ruling):
   wildcards would blunt the staleness check that is the point; the
   churn friction on new modules is intended. A short comment per
   BLOCK records the rationale; `runtime-budget` entries name the
   measured number. Guard checks: token validity; pair still derives
   as a gap (staleness both ways); `edge-consumer` entries
   additionally MACHINE-CHECKED (F2): the excluded suite must be
   selected by at least one other rule in `PATH_TEST_RULES` — an
   unselected edge-consumer is an orphan and reds. The guard does
   NOT re-run pytest (speed); fn-gated/runtime-budget truthfulness
   is review-time evidence anchored by the PR body's measurement
   table (spec scenario promises only what the guard checks: valid
   token + live pair — Note B).
3. **Routing rules honored, not overridden** (the #1452 audit's
   standing calls): (a) orchestrator whole-suite redirect targets —
   suites the `stop_on_match` redirect design deliberately swaps for
   focused nodes — stay excluded as `redirect`; (b) cross-surface
   importers (e.g. orchestration-layer suites importing
   `workers/data_adapters/base.py`) are `edge-consumer`: they belong
   to the orchestrator-side rules that own those suites, and the gap
   closes from that side or not at all — copying them into adapter
   rules couples unrelated PR classes; (c) the
   `services/production_closure` lane family closes with ONE rule
   (or directory-rule extension) covering
   `test_two_node_e2e_evidence.py` IF measurement shows it executes
   real assertions in the PR lane — 844 collected items suggest
   material runtime; if `passed == 0` it is `fn-gated`, if heavy it
   is `runtime-budget` with the number.
4. **Rule shape per directory**: narrow per-module rules
   (`real_backend.py` precedent) for modules with material distinct
   gaps; directory-list extension only where every module in the
   directory shares the addition (tile_publisher's single suite).
   Constraints checked per addition (F1 corrected): extending an
   existing rule happens AT THE RULE SITE via
   `(*SHARED_CONST, "tests/new.py")` — NEVER by editing a shared
   constant (`FILE_JOURNAL_READ_STATE_TESTS` leaks to
   `packages/common/safe_fs.py`); for `scheduler.py` either extend
   an existing entry at-site or add a third allowlisted entry placed
   BEFORE the index-11 stop rule (the duplicate guard counts
   allowlisting, not entry count); placement before any
   `stop_on_match` rule that matches the same paths (order-audit
   recorded in the PR body); every added target verified non-gated
   at file level and existing. Package-`__init__.py` rows (34 of the
   211 pairs, Note D) are re-export shims closable cleanly with
   narrow per-`__init__` rules — prefer ADD for them; none of the
   four tokens describes them as exclusions.
4b. **Positive-selection floor** (F2 — the guard alone is
   satisfiable by 211 exclusions and zero additions, which would
   re-create the rotted-audit failure this change ends): explicit
   `select_tests` assertions pin, at minimum, the audit-confirmed
   same-name direct gaps as ADDED:
   `services/tile_publisher/publisher.py`→`test_cli_publish_qdown`;
   `workers/output_parser/{cli,parser}.py`→`test_output_parser_cli`
   + `test_output_parser_dual_write`;
   `workers/shud_runtime/runtime.py`→`test_warm_start` +
   `test_warm_start_chaining`;
   `services/slurm_gateway/{app,gateway}.py`→
   `test_role_boundary_static` / `test_monitoring_api` +
   `test_retry_cancel_consistency`; orchestrator same-name family
   (persistence, retry, reconcile, scheduler_generation,
   scheduler_timing, replay_lineage, retention, run_tree_copyback →
   their same-name suites). These are floor pins, not a frozen
   ceiling — the guard still governs the rest.
5. **Guard implementation**: inverted-index derivation (parse each
   tracked top-level test file once → imported dotted names; invert;
   per gap module call the selection once). Target < 5 s (measured
   1.15 s for the probe). "Selected" is the NODE-LEVEL convention
   (F7): a suite reachable only as `::`-qualified node ids from a
   redirect rule still counts as a gap (partial coverage ≠ suite
   coverage) — stated here and pinned by a test; normalizing `::`
   would silently shrink the map by 7 pairs (6 orchestrator,
   1 forcing_producer). The derivation accepts an injectable
   selection callable or rules tuple (F8 — `select_tests` reads the
   module-global table, so the constructed-input red evidence for
   "stale because now selected" needs the seam; monkeypatching the
   global is the fallback, but say so in the test). Domain (F9): the
   guard covers every tracked module UNDER the nine directory paths
   — including modules owned by earlier stop rules (`chain.py`,
   `scheduler.py`, `cli.py`, `direct_grid_contract.py`…), which are
   in the gap map despite never reaching the directory rules.
   Anti-vacuity (F6): anchored on the PRE-subtraction importer
   universe (module→non-gated-importer pairs nonzero AND all nine
   directories contributed ≥1 module) — the residual gap set
   legitimately shrinks toward zero on the good outcome and must
   not be the anti-vacuity anchor. Green ⟺ every pair selected xor
   excluded.
6. **Runtime budget evidence** (F5 tightened): after rule growth,
   measure the selected-suite wall-clock for the heaviest touched
   module classes (at minimum `services/orchestrator/chain.py`,
   `workers/data_adapters/base.py`,
   `workers/forcing_producer/producer.py`, and one
   production_closure lane file) AND one union measurement for a
   representative multi-file PR shape (e.g. chain.py + scheduler.py
   + persistence.py together). Hard line: ~5 min local per single
   module (the "hosted ≈ 2× local" factor is a working assumption,
   recorded as unverified; 5 min × 2 × a few-module union still
   clears the 35-min cap with margin) — anything beyond routes to
   `runtime-budget` with the number.
7. **Spec delta**: ADDED requirement for the disposition guard
   (first line SHALL; domain wording "every tracked module under the
   nine audited directory paths" per F9; scenarios promise only what
   the guard checks per Note B). MODIFIED "Guarded-module selector
   rules MUST cover their non-gated importer closure": append a
   DESCRIPTIVE cross-reference sentence citing the ADDED requirement
   by its exact name (Note C — one normative source, no second
   SHALL), byte-faithful otherwise.

## Must preserve

- All 75 existing selector-suite tests green UNMODIFIED (comment-only
  edits allowed where a rule block gains entries next to them).
- Selection for every input class outside the 9 directories
  byte-identical (whole-tree old-vs-new diff is the oracle; expected
  delta = exactly the modules whose rules grew).
- Existing rules' targets never removed; `stop_on_match` semantics of
  existing rules unchanged; `CHANGED_TEST_FILE_RULES` untouched.
- PR #1486 guards stay green: duplicate-pattern allowlist (any new
  deliberate duplicate needs allowlist + rationale), marker anchor,
  pytest collection anchor, meta-guard pins.
- `--github-output` fields and stdout format unchanged.

## Seams under test

Existing pure-function seams: `select_tests` with real tree,
guard helpers over tracked files, exclusion table as plain data. Red
evidence for the new guard: parameterized rule-list/exclusion-table
inputs (constructed lists — the PR #1486 P2-7 pattern), never tracked
mutation.

## Test plan (maps to acceptance)

1. Disposition guard green on the final table+rules; red when: one
   added rule entry removed (names the pair); one exclusion removed;
   one exclusion made stale (fed a constructed rule list that now
   selects it); invalid token.
2. Order-audit pin: for each module gaining a narrow rule that
   coexists with a stop rule, a selection test proving the narrow
   rule's targets actually appear in `select_tests([module])`.
3. Whole-tree diff (implementer, recorded): delta = exactly the
   modules with grown rules, nothing else.
4. Issue #1455 Verification commands green.

## Risks to watch

- The 211-pair triage is the bulk; the measurement table is the
  honesty anchor — no disposition without its measurement row.
- Do not let the guard quietly re-implement PR #1486's per-module
  closure guard for the two GUARDED_MODULE_CLOSURES modules — the
  two guards overlap on `services/slurm_gateway`; make the domains
  explicit (this guard covers directory-rule-owned modules' DIRECT
  importers; the #1486 guard additionally does one-hop for its two
  modules).
- Guard wall-clock: keep the inverted index; do not call
  `_non_gated_top_level_importer_tests` per module.
