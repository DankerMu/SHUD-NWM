# Proposal — partition-oversized-reconcile-suites (#3177)

Closes #3177.

## Why

Six test suites on master are over the 1000-line large-file guard. Five are exempted in `.large-file-guard.json`
(`:116` and `:149-152`). The sixth, `inventory`, has never been exempted, so any commit that stages it is blocked:

| suite | lines | guard |
|---|---:|---|
| `tests/test_gateway_reconcile_claimant_exclusivity.py` | 3432 | exempted (#3155) |
| `tests/test_gateway_reconcile_binding_provenance.py` | 2298 | exempted (#3155) |
| `tests/test_gateway_reconcile_comment_accounting.py` | 1904 | exempted (#2674) |
| `tests/test_forcing_submit_ambiguity.py` | 1202 | exempted (#3155) |
| `tests/test_gateway_reconcile_file_cohort_identity.py` | 1137 | exempted (#3155) |
| `tests/test_gateway_reconcile_inventory.py` | 1083 | not exempted (issue addendum) |

- `openspec/specs/gateway-reconcile-test-partitioning/spec.md` names five of these as tracked by #3177, and permits
  the four exemptions only temporarily.
- The issue addendum says the end state is "every module at most 1,000, no exemption". That is only reachable if
  `comment_accounting` is partitioned too, so it is in scope.
- `test_forcing_submit_ambiguity.py` is not a gateway-reconcile suite, but it is in the issue table and has a #3155
  exemption.

## What

1. **Partition each suite** into collectible modules of at most 1000 lines. Each is a pure move, one commit per suite:
   - tests and helpers keep their original relative order within each partition;
   - every `def`/`class` line, decorator and body stays byte-identical;
   - only import blocks and the module docstring may differ.

   The base path keeps the first lane, as in the #3165 precedent, so existing references to the base path still
   resolve. The suggested lanes are below. A boundary may move by whole tests to fit the 1000-line budget, but tests
   never cross lanes out of order.

   Each row lists the suite's partitions. The base path keeps the first lane, and line ranges are on master.

   | suite | partition (line range on master) |
   |---|---|
   | `claimant_exclusivity` | base `tests/test_gateway_reconcile_claimant_exclusivity.py` `:192-1078` (claimant races, recycled ids, typed/normal commit races, row order, malformed/orphan anchors) |
   |  | `tests/test_gateway_reconcile_claimant_incarnation.py` `:1079-1772` |
   |  | `tests/test_gateway_reconcile_claimant_stale_authority.py` `:1773-2538` |
   |  | `tests/test_gateway_reconcile_claimant_replay_handoff.py` `:2539-end` |
   | `binding_provenance` | base `tests/test_gateway_reconcile_binding_provenance.py` `:149-756` (projection and settled-sibling provenance, wrong-kind payload) |
   |  | `tests/test_gateway_reconcile_binding_provenance_normalizer.py` `:757-1193` |
   |  | `tests/test_gateway_reconcile_binding_provenance_replay.py` `:1194-1716` |
   |  | `tests/test_gateway_reconcile_binding_provenance_legacy.py` `:1717-end` (illegal shapes, explicit-provenance normalizers, legacy v1) |
   | `comment_accounting` | base `tests/test_gateway_reconcile_comment_accounting.py` `:31-715` (versioned coverage, index comment sacct, file-submit attempt commit) |
   |  | `tests/test_gateway_reconcile_comment_fallback_bind.py` `:716-1384` |
   |  | `tests/test_gateway_reconcile_comment_fallback_guard.py` `:1385-end` |
   | `forcing_submit_ambiguity` | base `tests/test_forcing_submit_ambiguity.py` `:1-905` |
   |  | `tests/test_forcing_submit_ambiguity_reservation.py` `:906-end` |
   | `file_cohort_identity` | base `tests/test_gateway_reconcile_file_cohort_identity.py` `:1-535` |
   |  | `tests/test_gateway_reconcile_file_cohort_identity_terminal.py` `:536-end` |
   | `inventory` | base `tests/test_gateway_reconcile_inventory.py` `:1-549` |
   |  | `tests/test_gateway_reconcile_inventory_crash_migration.py` `:550-end` |

   Support modules, created only where a helper is shared:
   - `tests/gateway_reconcile_claimant_exclusivity_helpers.py`
   - `tests/gateway_reconcile_binding_provenance_helpers.py`
   - `tests/gateway_reconcile_comment_accounting_helpers.py`
   - `tests/gateway_reconcile_file_cohort_identity_helpers.py`
   - `tests/gateway_reconcile_inventory_helpers.py`
   - `tests/forcing_submit_ambiguity_helpers.py`

   The fsa `_forcing_members` and `_reserve_forcing` are defined after `:906` but used by the base, so they go to the
   fsa support module. Decorators and section comments that precede a `def` (1-4 lines) move with it.

   All new collectible names start with `tests/test_gateway_reconcile_` (gateway suites) or
   `tests/test_forcing_submit_ambiguity_`, so the issue's `tests/test_gateway_reconcile_*.py` verification glob still
   covers them.

   **Private helpers.**
   - A helper used by exactly one partition stays in that partition.
   - A helper used by two or more partitions of the same suite moves to a non-collectible support module named
     `tests/gateway_reconcile_<suite>_helpers.py`, or `tests/forcing_submit_ambiguity_helpers.py`. The name must not
     match `test_*.py` or `*_test.py`.
   - Same-named helpers in different suites (`_fallback_row`, `_fallback_querier` exist in three suites with different
     bodies) are never merged: each suite keeps its own copy.
   - No partition imports another partition.
   - Module-level constants (`_CYCLE*`, `_STALE_LAYOUTS`, `IFS_JOB`, ...) follow the same rule.

   **Cross-file importers.** These files import `_fallback_querier`, `_fallback_row` and
   `_second_master_reservation_record` from `tests.test_gateway_reconcile_claimant_exclusivity`, and are re-pointed to
   the claimant helper module:
   - `tests/orchestrator_bind_reserved_job_helpers.py:33`;
   - `tests/test_gateway_reconcile_submitline_exact.py:35`;
   - `tests/test_orchestrator_bind_reserved_job_cas.py:63`;
   - `tests/test_orchestrator_bind_reserved_job_lane.py:40`.

   No other partition or suite imports from any of the six suites (`git grep "from tests.test_..."`, checked on master).

   **`pyproject.toml` per-file-ignores.** `select` includes `BLE` (`pyproject.toml:77`), and each `BLE001` entry
   follows the partition that holds its `except Exception:` sites:
   - `claimant_exclusivity`: `:2755`, `:2946`, `:3084`, `:3106`, `:3190` and `:3347` are all in
     `_claimant_replay_handoff`, so the entry moves to that partition, and the base no longer needs it;
   - `inventory`: `:921` and `:999` (`except Exception: pass`) are in `_inventory_crash_migration`, so the entry moves
     there, and the base no longer needs it;
   - before the move, ruff must be red on each partition that carries these sites without its entry. This is evidence
     that the entry is load-bearing.

   None of the six suites defines a `pytest.fixture`, so no F401/F811 ignore is expected. If one turns out to be
   needed, it is added per file.

2. **Remove the five `.large-file-guard.json` entries**: `:116` (`comment_accounting`) and `:149-152`
   (`forcing_submit_ambiguity`, `binding_provenance`, `claimant_exclusivity`, `file_cohort_identity`). Add none.
   `inventory` has no entry to remove.

3. **Selector.** Same policy, paths replaced in place:
   - Every rule, table, frozen list and pin that names a suite's base path also names each of its new partitions, in
     `scripts/select_ci_tests_rules.py`, `scripts/select_ci_tests_tables.py` and `tests/test_select_ci_tests.py`:
     rules (`:784-839`, `:1269`, `:1972-1982`, `:2353-2366`, `:2453`), tables (`:1147`, `:1312`, `:1446-1450`), and pins
     (`:1816`, `:1879`, `:1985-1992`, `:11735-11748`, `:11902`, `:13256-13281`, `:18083`).
   - Node-id references (`select_ci_tests_tables.py:1470-1471`, `tests/test_select_ci_tests.py:13278-13281`) name the
     partition that owns the test after the move.
   - `GATEWAY_RECONCILE_PARTITIONS` (`tests/test_select_ci_tests.py:11734`) gains the new partitions of
     `comment_accounting`, `file_cohort_identity` and `inventory`. It does not gain partitions of
     `claimant_exclusivity` or `binding_provenance`, which are not in it today. Its count comments (`:11731`, `:11762`,
     `:18080`) are updated.
   - **Runtime-budget dispositions are derived per partition, not inherited from the base.** A partition gets a
     `(reconcile.py | persistence.py, partition) -> runtime-budget` disposition only if the partition itself has the
     derived file-level importer edge. Otherwise it goes into the exclusion tuple next to the base (`:11896-11909`).
     - Known case: `_comment_fallback_bind` (`:716-1384`) does not use `SacctRecord`, whose uses are at
       `:114/352/481/541/1742/1870`. It therefore loses the file-level `reconcile` import and needs an exclusion.
     - The `inventory` partitions follow the base, which is already in the tuple.
     - The oracle is `test_directory_rule_importer_gaps_are_dispositioned`: no stale and no missing disposition.
   - The `runtime-budget` exclusions for `services/orchestrator/reconcile*.py` and `persistence.py` stay as narrow as
     today (spec "Runtime-budget exclusions remain narrow").
   - **Support-module routes.** Each new support module gets a `SUPPORT_MODULE_TEST_RULES` route to its collectible
     consumers plus `tests/test_select_ci_tests.py`, and one `SUPPORT_MODULE_ROUTING_ANCHORS` entry
     (`tests/test_select_ci_tests.py:13552`). `:14159` requires the anchors and the routes to be equal sets.
     - The claimant helper's route lists its consumer partitions and the three collectible cross-file importers:
       `test_gateway_reconcile_submitline_exact.py`, `test_orchestrator_bind_reserved_job_cas.py` and
       `test_orchestrator_bind_reserved_job_lane.py`.
     - It also lists the collectible consumers of `tests/orchestrator_bind_reserved_job_helpers.py`, the
       non-collectible module that imports the claimant helper, following `select_ci_tests_rules.py:756-766`. A route
       never names a non-collectible module.
   - Line budgets: the three selector files are already guard-exempt (`.large-file-guard.json:69,70,76`).

4. **`scripts/ci/full_test_durations.json`.** Each base weight is split over the base and its partitions, rounded to
   3 decimals, keys kept sorted (`tests/test_ci_shard_tests.py:172-178` pins sorted `tests/*.py` keys).
   - The split cuts the existing CI-measured base weight, so absolute local timings are never mixed into the CI-measured
     table. The ratio comes from measured per-file seconds in the E5 local run (`--junit-xml`, summed per file) when
     available. Otherwise it is proportional to collected case count.
   - `scripts/ci/shard_tests.py:93-106` weights unknown files with the median (0.678 s) and ignores stale keys.
     `file_cohort_identity` weighs 59.8 s, most of it probably in the `:536` parametrized batch test, so the default
     would skew the master shards.

5. **Spec `gateway-reconcile-test-partitioning`.**
   - REMOVE "Gateway reconcile tests remain complete under physical partitioning" and ADD "Gateway reconcile tests stay
     within the line guard without exemptions" (REMOVED + ADDED, because the scenarios change).
   - The new requirement states the collection-identity rules without a hard case count.
   - Its scenarios:
     - every `tests/test_gateway_reconcile_*.py` module and gateway-reconcile support module is at most 1000 lines;
     - `.large-file-guard.json` has no gateway-reconcile entry;
     - a re-partition keeps the `::test_name[param-id]` suffix set byte-identical.
   - The "538" figure predates several suites and is dropped, not restated.

## Non-goals

- No production source change.
- No assertion, oracle, test name, parametrization or decorator change.
- No pytest discovery configuration change.
- No other oversized suite is touched.
- No new guard exemption.
- No change to `GATEWAY_RECONCILE_PARTITIONS` membership policy for `claimant_exclusivity`, `binding_provenance`,
  `submitline_exact` or `reserved_pins`.

## Must preserve

- Per suite, the sorted `::test_name[param-id]` suffix set over the base path plus its partitions equals the base path's
  suffix set before the move.
- Every old suffix maps to exactly one new full node id.
- Per suite, the per-definition fingerprints of the old file equal the multiset over its partitions and its support
  module: every definition appears exactly once, and none is duplicated. A fingerprint is the AST dump plus the sha256
  of the source segment, decorators included, of every top-level `def`/`class` and module-level assignment.
- Selection: see E3 for the exact equivalence and its allowed differences.
- The full `tests/test_gateway_reconcile_*.py` run, `tests/test_forcing_submit_ambiguity*.py`, the three collectible
  cross-file importers, the collectible consumers of `tests/orchestrator_bind_reserved_job_helpers.py` and
  `tests/test_select_ci_tests.py` stay green.

## Fixture level

expanded, with design.md. Legacy compatibility is an expanded trigger, and the precedent #1809 was expanded. Seats:
- correctness: collection identity, helper ownership, selector equivalence;
- test evidence + spec compliance;
- invariant: selector derivation and dispositions.

## Evidence Floor

- [ ] E1 Lint, line counts and guard entries:
  - `uv run ruff check --no-cache .` passes;
  - `git diff --check` is clean;
  - the entropy hard gate reports `hard_gate_failing_count 0`;
  - `wc -l` is ≤1000 on every new or touched test or support module that is not guard-exempt;
  - `.large-file-guard.json` has lost exactly the five entries and gained none;
  - ruff is red on `_claimant_replay_handoff` and `_inventory_crash_migration` without their moved `BLE001` entries.
- [ ] E2 Collection identity. Baseline at the branch point, under Python 3.11: per suite, the `--collect-only -q`
  suffix set and the per-definition fingerprint multiset. On each suite's pure-move commit:
  - the suffix set is equal to the baseline;
  - the fingerprint multiset is equal to the baseline, every definition exactly once;
  - the old suffix → new node id TSV is recorded, with no duplicates and no misses.
- [ ] E3 Selector equivalence. For every path tracked on master, `select_tests([path])` is computed before and after.
  The only allowed difference is each old base path expanding to the base plus its partitions. The other allowed
  differences are listed:
  - (a) A diff to the claimant base no longer selects exactly these three through the importer closure
    (`scripts/select_ci_tests.py:413-415`), because they now import the claimant helper module:
    `tests/test_gateway_reconcile_submitline_exact.py`, `tests/test_orchestrator_bind_reserved_job_cas.py` and
    `tests/test_orchestrator_bind_reserved_job_lane.py`. The helper module's route covers them, and E3 shows that a
    diff to the helper module selects them. The bind helper's other consumers were not selected by a claimant diff
    before either: `tests/orchestrator_bind_reserved_job_helpers.py` is not a suite, so it is not in the importer
    index. They appear only in the helper route's after-set, under (b).
  - (b) New paths (partitions, support modules) have no "before". For them, E3 records the after-set and checks it:
    - a partition selects itself plus the other partitions of its suite only where a redirect rule says so;
    - a support module selects its route plus `tests/test_select_ci_tests.py`.
  - (c) When a base path itself is the changed path, it selects itself, the meta-guard and its importers. It does
    not select its sibling partitions, because a changed suite selects only itself (`scripts/select_ci_tests.py:413-415`).
    The base → base + partitions expansion therefore applies to every other path that selected the base, but not to
    the six base paths themselves.
  - (d) The two node ids at `select_ci_tests_tables.py:1470-1471` (`comment_accounting` `:364` and `:497`) stay in the
    base, so they are unchanged.

  Also:
  - the list of paths whose selection changed is recorded;
  - each new partition and support module has a pin that is red before its rule is added;
  - a tracked-tree guard pins, per suite, an explicit tuple of its base plus its partitions, by exact path. A glob is
    not enough: `test_gateway_reconcile_comment_*` also matches `comment_capability` and `comment_sacct_bounds`.
    The guard compares against the tracked tree:
    - the tracked `tests/test_gateway_reconcile_*.py` set equals `GATEWAY_RECONCILE_PARTITIONS` ∪ the claimant and
      binding tuples ∪ {`submitline_exact`, `reserved_pins`};
    - the tracked `tests/test_forcing_submit_ambiguity*.py` set equals the fsa tuple;
    - so an untupled extra file goes red.
- [ ] E4 Mutation reds (each restored):
  - one test dropped from a partition: E2 suffix identity red;
  - a stray test added to a partition: E2 suffix identity red;
  - a helper copied into two partitions: E2 fingerprint multiset red;
  - one partition removed from a rule: E3 pin red;
  - an extra `tests/test_gateway_reconcile_claimant_extra.py` (collectible, one trivial test) added and tracked:
    tracked-tree guard red.
- [ ] E5 Locally and in a node-27 temporary worktree:
  - `tests/test_gateway_reconcile_*.py` and `tests/test_forcing_submit_ambiguity*.py`;
  - the three collectible cross-file importers, and the collectible consumers of
    `tests/orchestrator_bind_reserved_job_helpers.py`;
  - `tests/test_select_ci_tests.py`;
  - the selector set for the diff. New partitions are `git add`ed before the selector suite runs, because the importer-closure
  derivation reads tracked files only.
- [ ] E6 `openspec validate partition-oversized-reconcile-suites --strict --no-interactive`.
