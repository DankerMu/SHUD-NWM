## Fixture

Fixture level: expanded · repair intensity: medium · project profile: NHMS (`openspec/project-profile.md`)

Change surface:
- `scripts/select_ci_tests.py`: `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` rule site for `scheduler_runtime.py`; new `packages/common/copyback_guard.py` rule; `services/precip/**` target; `PathTestRule` comment + `_rule_activated` docstring.
- `tests/test_select_ci_tests.py`: routing pins; `PATH_TEST_RULES` table guard; #1656 block; #1913 registry-partition block.
- New `tests/fixtures/basins_registry_partition_additions.json`.
- `openspec/changes/archive/2026-09-11-route-retention-deletes-through-copyback-mutex/tasks.md` Known limits note.

Must preserve (baseline measured on `55a14398d`, `printf '<path>\n' | uv run python scripts/select_ci_tests.py | wc -l`):
- `services/orchestrator/retention.py` 49 · `services/orchestrator/cli.py` 30 · `services/orchestrator/__init__.py` 48 · `tests/retention_test_helpers.py` 6 — selections byte-identical after the change.
- `services/orchestrator/scheduler_runtime.py` 22 → exactly the same 22 plus the mutex suite (23).
- `packages/common/copyback_guard.py` 9 → exactly the same 9 plus the mutex suite (10).
- `services/precip/constants.py` / `services/precip/mirror.py` 6 → the same 6 plus `tests/test_node27_raw_retention.py` (7). `apps/api/routes/precip.py` 7 → unchanged.
- Every other `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` path keeps its selection (the shared constant is not edited).
- Existing exact pins that legitimately move, and only by the named suite: `test_select_tests_maps_file_journal_read_state_without_whole_legacy_suites` (+ mutex suite); `test_precip_tree_module_selects_the_prewarm_reader_suite` (+ raw-retention); `STOP_RULE_AT_SITE_EXTENSIONS` (+ scheduler_runtime row). New edge: the additions ledger → `tests/test_select_ci_tests.py`. Any other red pin is a regression, not an expectation update.
- `services/precip/cache.py` selection: 6 → 7 (+ raw-retention); spec #2098 requirement modified accordingly (pre-existing drift from #2122 corrected in the same delta).
- PR-lane cost: mutex suite (~5.3 s) added to `scheduler_runtime.py` / `copyback_guard.py` diffs; raw-retention suite (~0.1 s) to `services/precip/**` diffs.
- `PRECIP_SURFACE_TESTS` membership; the four `CHANGED_TEST_FILE_RULES` `only_when_any_changed` gates and `test_changed_test_rule_activation_predicate_matches_production`.
- `tests/fixtures/basins_registry_partition_oracle.json` byte-identical; `REGISTRY_PARTITION_FROZEN_DIGESTS`, `REGISTRY_PARTITION_CONTRACT_SHA256`, `REGISTRY_PARTITION_BASELINE_SOURCE_SHA256`, #1903 transition literals unchanged; every existing #1913 guard stays green on the live tree with an empty ledger.
- Zero diff to `tests/test_timescale_write_guard_wire_site_invariant.py`, `TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS`, the seven registry partitions, and the registry helper.

Seams under test: `select_tests(changed, repo_root=Path("."))` (public selector function); #1656 `_invariant_scan_roots` on a repo-shaped tmp copy (existing idiom); #1913 pure checkers over injected text, suffix lists, count strings and git/blob readers.

Risk packs (core):
- Public API / CLI / script entry: selected — `select_ci_tests.py` is the PR-lane dispatcher; evidence 1.x/2.x exact-set pins.
- Config / project setup: not selected — no config/env files change.
- File IO / path safety / overwrite: not selected — tests read tracked files and write only under `tmp_path`.
- Schema / columns / units / field names: selected — new additions-ledger JSON schema; evidence 5.2/5.4.
- Auth / permissions / secrets: not selected — none touched.
- Concurrency / shared state / ordering: selected (narrow) — rule ordering / `stop_on_match` first-match semantics; evidence 1.3 must-preserve rows.
- Resource limits / large input / discovery: not selected — the #1913 structural-limit guard is preserved unchanged; demo target avoids `_qhh.py`.
- Legacy compatibility / examples: selected — existing pins and frozen oracle must stay green/unchanged; evidence "Must preserve".
- Error handling / rollback / partial outputs: selected — guards must fail loudly with named messages; evidence 4.2/5.3.
- Release / packaging / dependency compatibility: not selected — no dependency change.
- Documentation / migration notes: selected — #1913 rationale + addition procedure; archived Known limits; spec deltas.
Domain packs (profile): none selected — no forecast, geometry, DB, SHUD or Slurm surface.

Non-goals:
- No production runtime change; no CI workflow change; no edit to `FILE_JOURNAL_READ_STATE_TESTS` or `PRECIP_SURFACE_TESTS`.
- `tests/test_retention_extra_roots.py` scheduler_runtime gap (#2260 sibling) — known limit. **Closed by #2316** (change `close-selector-reader-routing-gaps`).
- Replacing bare literals in `tests/test_node27_raw_retention.py` (#2191 out of scope).
- `only_when_any_changed` honouring on `PATH_TEST_RULES` (#2198 option a) and type split (option c).
- QHH `qhh_bootstrap_partition_oracle` sibling freeze (#2183 out of scope).
- Migrating #2158's test back into a partition; adding any placeholder test to a partition.

## 1. #2260 copyback mutex routing

- [x] 1.1 Extend the `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` rule site whose pattern equals `services/orchestrator/scheduler_runtime.py` with `tests/test_retention_copyback_mutex.py` (site comment: stop rule shadows the orchestrator tree rule).
- [x] 1.2 Add non-stop path-exact rule `packages/common/copyback_guard.py` → `("tests/test_retention_copyback_mutex.py",)`.
- [x] 1.3 Pins in `tests/test_select_ci_tests.py`, written like the existing #2238 retention pins: `scheduler_runtime.py` → exact 23-element literal list (the list in the spec delta scenario); `test_select_tests_maps_file_journal_read_state_without_whole_legacy_suites` expected set + mutex suite with comment; `STOP_RULE_AT_SITE_EXTENSIONS` scheduler_runtime row in the shape that pin requires; `copyback_guard.py` → exact 10-element literal list (`tests/test_api.py, tests/test_copyback_guard.py, tests/test_gateway.py, tests/test_migrations.py, tests/test_orchestration_chain.py, tests/test_production_scheduler.py, tests/test_retention_copyback_mutex.py, tests/test_river_segment_write_surface_scan.py, tests/test_select_ci_tests.py, tests/test_timescale_write_guard_wire_site_invariant.py`); retention/cli/`__init__`/helpers counts unchanged (existing pins stay green unedited, or a count pin if none exists). Red-proof: each new pin red on pre-change selector source.
- [x] 1.4 Archived `openspec/changes/archive/2026-09-11-route-retention-deletes-through-copyback-mutex/tasks.md` Known limits: append a "closed by #2260" note to the `copyback_guard.py` entry (~:302) and to the `scheduler_runtime.py` entry (~:351); state the `tests/test_retention_extra_roots.py` sibling stays open. Also refresh the stale "(follow-up #2198)" wording in `test_precip_composition_owner_rules_carry_neither_selection_flag` docstring to point at the new table guard.

## 2. #2191 precip raw-retention edge

- [x] 2.1 Widen `services/precip/**` own target with `tests/test_node27_raw_retention.py`; comment names the one-hop chain and the second-order-rename rationale.
- [x] 2.2 Update `test_precip_tree_module_selects_the_prewarm_reader_suite` literal list 6 → 7 (both params); `test_precip_route_rule_stays_without_the_prewarm_suite` unedited and green; add a route-rule assertion that `tests/test_node27_raw_retention.py` is absent if the existing exact list does not already imply it (it does — no edit needed). Red-proof: updated pin red on pre-change source.

## 3. #2198 inert `only_when_any_changed`

- [x] 3.1 Table-level test: every `PATH_TEST_RULES` row has empty `only_when_any_changed`; failure message names the offending pattern(s).
- [x] 3.2 Constructive proof: monkeypatch `PATH_TEST_RULES` with one row given a gate (e.g. `services/precip/**` + `("scripts/select_ci_tests.py",)`) → the same checker returns a violation naming `services/precip/**`.
- [x] 3.3 Comment on `PathTestRule.only_when_any_changed` and scope sentence in `_rule_activated` docstring: honoured only on `CHANGED_TEST_FILE_RULES`; rejected by guard on `PATH_TEST_RULES` and `SUPPORT_MODULE_TEST_RULES`.

## 4. #2230 #1656 meta-guard discriminating power

- [x] 4.1 In `test_supplemental_invariant_routing_reds_when_a_root_is_dropped`, keep the violations assertion and add, under the monkeypatch: `INVARIANT_SUITE_PATH not in select_tests(["scripts/brand_new_thing.py"])` and `INVARIANT_SUITE_PATH in select_tests(["workers/brand_new_thing.py"])`. Evidence: deleting the `monkeypatch.setattr` line locally reds the test (run once, output recorded, line restored).
- [x] 4.2 `_invariant_scan_roots`: assert exactly one top-level `ast.Return` in `_scan_roots` body with a named message; fix the "final return" comment. Constructive red test: tmp_path repo-shaped copy with a second identical top-level `return` → `pytest.raises(AssertionError, match=<named message>)`.
- [x] 4.3 `uv run pytest -q tests/test_select_ci_tests.py -k "supplemental_invariant or invariant_suite_literal"` green.

## 5. #2183 registry partition addition ledger (implementation pass 2)

- [x] 5.1 Add `tests/fixtures/basins_registry_partition_additions.json` = `{"schema": "basins-registry-partition-additions/v1", "additions": []}`; a test asserts it is git-tracked (mirror `test_registry_partition_oracle_is_tracked_and_anchored`); oracle JSON byte-identical (`git diff --exit-code tests/fixtures/basins_registry_partition_oracle.json`). Route it: path-exact `PathTestRule("tests/fixtures/basins_registry_partition_additions.json", ("tests/test_select_ci_tests.py",))` with neither flag, pinned `select_tests([ledger]) == ["tests/test_select_ci_tests.py"]` (red on pre-change selector, which selects nothing).
- [x] 5.2 Ledger integrity checker — pure function over `(oracle, ledger, git/blob reader)` with every rule of design D5 "ledger integrity" as a named assertion. Tests, each → named failure: wrong schema; missing key; empty `nodes`; node not prefixed by `name`; node overlapping frozen suffixes / another record; `integration_nodes ⊄ nodes`; duplicate name; name ∈ frozen rows; owner = retained core; owner = helper; `row[0]` ≠ owner (`ledger.row_owner_mismatch`, incl. row[0] = retained core under an allowed owner); malformed row (`ledger.row_shape`); integration nodes under a non-database-authority owner (`ledger.integration_owner_not_database_authority`); unknown `base_commit`; non-ancestor `base_commit` (built with an injected git reader, not a checkout-dependent SHA); owner blob absent at `base_commit`; name present at `base_commit`. Valid record (injected reader) → no violation.
- [x] 5.3 Re-base collection / integration+owner-count / definitions / execution guards on `frozen ∪ additions` per design D5 per-site list (96 literal, suffix digests over observed minus additions, `owner_of` incl. ledger owners, execution deltas; bug008 untouched). Live tree with empty ledger → every existing #1913 guard green with today's literals. Unregistered-definition failure message includes the observed row tuple.
- [x] 5.4 Constructive proofs on pure checkers (target text = `tests/test_basins_registry_import_db.py` content + an appended real-shaped test, in memory / tmp text): (a) no record → definitions checker red naming `tests/test_basins_registry_import_db.py::<name>` and printing the row; (b) + correct record → definitions checker green, and collection/integration/owner-count expected-value functions accept the injected observed suffix lists; (c) record present, body edited → red "drifted"; (d) frozen definition body edited with a valid record for another name → red; (e) record naming a frozen definition → ledger red; (f) execution expected strings for one non-integration + one integration addition → `"79 passed, 19 skipped"`, `"79 passed, 1 skipped, 18 deselected"`, `"18 skipped, 80 deselected"`; empty ledger → the three frozen literals; (g) deletion: a frozen definition removed from the DB partition text, and a registered addition absent from the tree, each red naming `tests/test_basins_registry_import_db.py::<name>` in the definitions, collection and integration checks. Every tree-reading proof unions its records with the real ledger, re-run under a simulated non-empty ledger (in-memory auth.py addition).
- [x] 5.5 Tamper proof for "never backfill from the tree": with an unchanged ledger, adding a def to the checked text cannot turn the definitions/collection checks green; the ledger checker obtains the before-state only via the git blob reader (asserted by injecting a reader and observing it is the only source consulted for `base_commit` content).
- [x] 5.6 Rewrite the #1913 rationale header: generator/contract under `.workplans/issue-1913/` is gone (optional arm is a no-op when absent); add the addition procedure (append test → run guards → copy printed row/nodes into a ledger record with `issue` and `base_commit` = merge-base → review). Amendment of existing definitions keeps the #1903 transition shape.

## 6. Verification (Evidence Floor)

- [x] 6.1 `printf 'services/orchestrator/scheduler_runtime.py\n' | uv run python scripts/select_ci_tests.py | grep tests/test_retention_copyback_mutex.py`
- [x] 6.2 `printf 'packages/common/copyback_guard.py\n' | uv run python scripts/select_ci_tests.py | grep tests/test_retention_copyback_mutex.py`
- [x] 6.3 `printf 'services/precip/constants.py\n' | uv run python scripts/select_ci_tests.py` contains raw-retention; `printf 'apps/api/routes/precip.py\n' | …` does not.
- [x] 6.4 Must-preserve count rows re-measured (retention 49, cli 30, `__init__` 48, helpers 6, route 7).
- [x] 6.5 `uv run pytest -q tests/test_select_ci_tests.py` green; `uv run pytest -q tests/test_node27_raw_retention.py tests/test_retention_copyback_mutex.py` green.
- [x] 6.6 `uv run ruff check .` green; `openspec validate close-selector-gate-fixture-gaps --strict --no-interactive` green.
- [x] 6.7 `git diff --stat origin/master` shows no production runtime file, no `.github/` file, no partition/helper/invariant-suite file, and the frozen oracle unchanged. (Measured against merge-base `55a14398d`; origin/master has since advanced with #2307, whose own files appear in a literal `origin/master` diff — re-measure after rebase.)

## Known limits

- `tests/test_retention_extra_roots.py` is still not selected by a `scheduler_runtime.py`-only diff (#2260 sibling leg; issue boundary). **Closed by #2316** (change `close-selector-reader-routing-gaps`): the same stop rule is extended at its site with the suite.
- #2183: no real partition addition ships; the ledger is proved only by pure-checker constructive proofs (injected text/lists/readers) until the first genuine addition uses it.
- The frozen oracle `tests/fixtures/basins_registry_partition_oracle.json` itself still routes to nothing in the PR lane (pre-existing; it is not meant to be edited).
- The QHH partition freeze (#1948) has the same no-addition shape and is untouched.
- #2183: the retained core `tests/test_basins_registry_import.py` is not a valid addition owner (bug008 literal command); additions whose non-integration nodes skip/xfail, or whose integration nodes do not skip, in the sandboxed child pytest are not registrable.
