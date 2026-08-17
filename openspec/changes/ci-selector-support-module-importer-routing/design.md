# Design: ci-selector-support-module-importer-routing

## Change surface

- `scripts/select_ci_tests.py` changed-test branch (:720-749 at
  538c02ce): non-suite `tests/` paths fall through
  `CHANGED_TEST_FILE_RULES` unmatched and map to
  `SELECTOR_META_GUARD_TEST` (:743), arming `meta_guard_only` and the
  ci.yml collect-only smoke. This change inserts a support-module
  rule lookup before that fallback.
- `tests/test_select_ci_tests.py`: hosts the derivation helpers
  (`_non_gated_top_level_importer_tests`, `is_test_suite_path` via
  the selector import) and the guard family this guard joins.

## Verified facts (orchestrator-derived at 538c02ce)

8 tracked non-suite `tests/` modules:

| module | derived non-gated importers | current select |
|---|---:|---|
| tests/fixtures/mapping_builder/in_memory_grid_snapshot.py | 5 (mapping_builder algorithm/binding/cli/evidence/integration) | meta-guard only |
| tests/slurm_template_helpers.py | 2 (production_slurm_validation, slurm_array_contract) | meta-guard only |
| tests/river_identity_backfill_fakes.py | 2 (node27_river_identity_backfill, …_receipt) | meta-guard only |
| tests/integration_helpers.py | 5 | meta-guard only (**carve-out, see decision 4**) |
| tests/conftest.py | 2 (`import tests.conftest` suites) | meta-guard only (**carve-out, see decision 4**) |
| tests/__init__.py | **3** (test_integration_gate + the two node27_timeseries_compression suites, 454 collected — `from tests import X` contributes base name `tests`, and the repo's `_dotted_module_name` deliberately aliases a package `__init__` to the package, pinned at test_dotted_module_name_maps_a_package_init_to_the_package) | meta-guard only |
| tests/fixtures/mapping_builder/keliya/build.py | 0 | meta-guard only |
| tests/mock_shud_omp.py | 0 | meta-guard only |

The 9 suites behind the issue's three named modules collect 549 tests
in ~0.4-0.7 s; `tests/__init__.py`'s 3 add 454 collected. Issue
#1487's derivation claimed 0 importers for `tests/__init__.py` — the
fixture review re-derivation (via the repo's actual authority helpers)
refutes that: the issue used a no-aliasing derivation the repo already
rejected. This fixture follows the repo authority: **6 of 8 modules
derive non-zero importers**, and `tests/__init__.py` is ROUTED like
the other three (fixture-review P1-1 ruling).

## Key decisions

1. **Routing via a dedicated `SUPPORT_MODULE_TEST_RULES` tuple**, not
   `CHANGED_TEST_FILE_RULES`: the latter's semantics are "changed
   test SUITE files" with `only_when_any_changed` conditions and a
   duplicate-guard exemption; mixing support-module entries in would
   muddy both contracts. New tuple of `PathTestRule` with exact-path
   patterns (no globs needed for 4 entries: the issue's three plus
   `tests/__init__.py` per P1-1), consulted in the changed-test
   branch only for paths where `is_test_suite_path` is False, AFTER
   the `CHANGED_TEST_FILE_RULES` loop (whose patterns are all suite
   basenames, so the domains are disjoint today — a comment states
   the interaction: support rules are reachable only when
   `matched_changed_test` is False AND the path is non-suite); on
   match, `selected.update(rule.tests)` and the meta-guard fallback
   is skipped. No `stop_on_match` semantics needed (exact paths are
   disjoint). Unmatched support paths keep the existing
   `SELECTOR_META_GUARD_TEST` fallback byte-for-byte.
2. **A matched support module selects its importer suites PLUS the
   meta-guard suite** (fixture-review P2-5 correction): the spec
   requirement "Changed-test PRs MUST run the selector meta-guards"
   exists so tree-derived meta-guards — including the new closure
   guard itself — run on exactly the PR class that can invalidate
   them; a routed support-module PR is such a class. Cost: one cheap
   suite. `meta_guard_only` is False either way (selection has other
   targets). The `is_test_suite` meta-guard addition at :747-748
   remains suite-only, untouched.
3. **Guard derivation authority is the existing helper family**:
   `_non_gated_top_level_importer_tests(module)` as semantic
   authority — dotted-name matching over top-level imports WITH the
   repo's deliberate package-`__init__`-to-package aliasing
   (`_dotted_module_name` strips `.__init__`; `tests/__init__.py`
   therefore derives 3, see Verified facts) — executed through the
   inverted index for wall-clock (decision under Risks). The guard
   iterates every tracked non-suite `tests/**/*.py` (classified by
   `is_test_suite_path`), and asserts:
   - module on the ISSUE_1487_SCOPE_CARVEOUT allowlist → skipped
     (see decision 4);
   - derived importers non-empty → `select_tests([module])` ⊇ derived
     set (missing suite named in the failure);
   - derived importers empty → `select_tests([module])` ==
     `[SELECTOR_META_GUARD_TEST]` exactly (no gratuitous selection).
   Anti-vacuity (fixture-review Note-8 hardened): assert the
   pre-allowlist derived universe is non-empty AND ≥ 3 modules
   derive non-zero importers (today: 6 of 8), AND per routed module
   anchor ONE known member (the GUARDED_MODULE_CLOSURES pattern —
   e.g. in_memory_grid_snapshot → test_mapping_builder_algorithm) so
   a half-blanked derivation cannot hide inside the aggregate count.
   Never a frozen filename/integer snapshot of the full required
   sets themselves.
4. **Carve-out allowlist is a recorded scope boundary, not a
   compensation claim** (fixture-review P2-4 correction):
   `ISSUE_1487_SCOPE_CARVEOUT_SUPPORT_MODULES = {"tests/integration_helpers.py", "tests/conftest.py"}`.
   Issue #1487 explicitly excludes both. The honest facts: ci.yml's
   `database` filter lists both paths and starts
   `real-db-integration`, but that job runs `pytest -q -m
   integration` — which covers 75 of integration_helpers' importers'
   245 collected tests and 0 of conftest's importers' 19 (measured;
   the derivation's non-gated set and `-m integration`'s set are
   near-disjoint by construction). So the carve-out is an inherited
   issue-scope decision with PARTIAL external coverage, recorded as
   such in the allowlist comment. The ci.yml pin (each path verbatim
   inside the `database:` filter block — block-scoped slice, not
   whole-file grep) anchors the factual predicate the scope decision
   cites; if the filter drops the path, the carve-out entry reds and
   forces a re-decision. Full routing for these two is a candidate
   follow-up, routed at Phase 8 if warranted.
5. **Red evidence via constructed inputs**: the guard's core
   comparator takes injectable `(module → derived set)` mapping and a
   selection callable, so red paths (missing importer suite; 0-importer
   module selecting extra targets; allowlist entry absent from ci.yml
   text) are provable with constructed inputs — zero tracked
   mutation. Honest labels where pins are naturally green.
6. **Docs third-mode line**: `instructions/agents/shared.md` (source)
   and the generated `CLAUDE.md`/`AGENTS.md` currently say the
   collapse fires for PRs that "只改 tests/ 支持模块如 conftest.py".
   Amend minimally: the collapse example remains true for conftest.py
   (compensated, still collapsing) and 0-importer support files; add
   the qualifier that importer-bearing support modules now select
   their importer suites instead. The three carriers MUST stay
   byte-identical on this line (existing repo invariant).
7. **Spec delta — TWO MODIFIED blocks + one ADDED** (fixture-review
   P1-2): ADDED requirement (first line SHALL) for the routing +
   mechanized closure + carve-out pinning + 0-importer meta-guard
   route. MODIFIED "Empty targeted-test selection MUST be loudly
   self-identifying": rescope the support-module clause to modules
   without derived importers + descriptive cross-reference. MODIFIED
   "Changed-test PRs MUST run the selector meta-guards": support
   modules still never self-select, but routed ones map to their
   importer suites PLUS the meta-guard suite while unrouted ones map
   to exactly the meta-guard suite (the "collectible test files"
   tail survives intact); the support-file scenario is rescoped the
   same way. Byte-faithful otherwise (difflib: zero substantive
   deletions beyond the surgical rewrites).
8. **Stale-target guard union extension** (fixture-review Note-7):
   `test_every_pinned_node_id_resolves_to_an_existing_test_function`
   (and any sibling that unions rule targets) gains
   `SUPPORT_MODULE_TEST_RULES`, so a deleted routed target reds
   instead of being silently dropped by the missing-target filter.

## Must preserve

- All 102 existing selector-suite tests green with EXACTLY ONE known
  rescope (fixture-review P1-3):
  `test_every_tracked_tests_support_module_selects_only_the_meta_guard_suite`
  (:1090-1109) asserts the inverse of the new routing and is rescoped
  to carve-out + 0-importer modules only, keeping its class-boundary
  assert at :1109; the stale prose at :1402-1407 is amended in the
  same commit. Any OTHER existing-test change is an undisclosed
  deviation.
- Whole-tree selection diff vs master: delta = EXACTLY the four
  routed support modules; everything else byte-identical (incl.
  `services/slurm_gateway/real_backend.py` and all suite-file
  inputs).
- `meta_guard_only` GitHub-output semantics, collect-only smoke
  labeling, `CHANGED_TEST_FILE_RULES`, `PATH_TEST_RULES`, duplicate
  allowlist, marker anchor, pytest collection anchor: untouched.
- `--github-output` fields and stdout format unchanged.

## Seams under test

`select_tests` with real tree; the guard comparator over injectable
mapping + selection callable; ci.yml read as text for the filter pin.
Red evidence via constructed inputs only (PR #1486 P2-7 pattern).

## Test plan (maps to acceptance)

1. Four routing pins: in_memory_grid_snapshot → ⊇ its 5
   mapping_builder suites; slurm_template_helpers → ⊇ its 2;
   river_identity_backfill_fakes → ⊇ its 2; tests/__init__.py → ⊇
   its 3 (P1-1 ruling; issue acceptance checkbox 6's "0-importer"
   example set is corrected to keliya/build.py + mock_shud_omp.py —
   recorded deviation from the issue text, evidence in Verified
   facts); each routed selection also contains the meta-guard suite
   (decision 2).
2. Closure guard green on live tree; red when: one routed suite
   removed from its rule (names it); a constructed new importer
   appears without a rule extension; a 0-importer module gains a
   gratuitous target; a carve-out entry vanishes from the ci.yml
   `database:` block.
3. Carve-out modules unchanged: select == [meta-guard] pinned for
   integration_helpers.py and conftest.py.
4. 0-importer route pinned: keliya/build.py and mock_shud_omp.py
   still select exactly the meta-guard.
5. Whole-tree diff (recorded): delta = the four routed modules only.
6. Issue #1487 Verification commands green.

## Risks to watch

- Guard wall-clock (fixture-review P2-6 measured): 8 per-module
  `_non_gated_top_level_importer_tests` calls = 8.35 s (NO caching
  exists); one `_non_gated_top_level_importer_index()` = 0.99 s. The
  guard MUST use the inverted index; the per-module helper stays the
  semantic authority (same predicate + marker filter).
- The ci.yml text pin must anchor inside the `database:` filter block,
  not anywhere in the file (a path listed under a different filter
  must not satisfy the pin).
- Do not let the new tuple leak into `_rule_selected_test_files` /
  disposition-guard domains (#1455's guard covers PATH_TEST_RULES
  only; keep domains explicit in comments).
