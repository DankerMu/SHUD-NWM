# Tasks — partition-oversized-reconcile-suites (#3177)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Legacy compatibility / examples | yes | pure move keeps every node, body and helper binding; cross-file importers re-pointed: E2, E5 |
| CI routing / test selection | yes | partitions and support modules in rules, tables, pins, anchors, `GATEWAY_RECONCILE_PARTITIONS`, per-partition runtime-budget dispositions, durations: E3, E4 |
| Resource limits / large input / discovery | yes | five guard exemptions removed, every module ≤1000 lines: E1 |
| Config / project setup | yes | `pyproject.toml` per-file `BLE001` moves and `.large-file-guard.json` entries removed: E1 (ruff red without the moved entries) |
| Documentation / migration notes | yes | spec REMOVED + ADDED: E6 |
| Others | no | no production code, DB, IO, concurrency or runtime change |

- [x] 1.1 Baseline at the branch point (Python 3.11): per suite, the collect-only suffix set and the per-definition
  fingerprint multiset.
- [x] 1.2 One pure-move commit per suite (proposal What 1). Each carries its partitions, its support module, the
  re-pointed cross-file importers (claimant only), its `pyproject.toml` per-file-ignores and the removal of its guard
  entry. Prove E2 on each commit.
- [x] 1.3 Selector commit (What 3) and durations split (What 4):
  - per-partition runtime-budget dispositions, support-module routes and anchors;
  - red-before-green pins and the per-suite tracked-tree guard;
  - partitions are tracked (`git add`) before `tests/test_select_ci_tests.py` runs.
- [x] 1.4 Evidence: E3 selector equivalence over every tracked path with the allowed differences, E4 mutation reds,
  E1 line counts and the ruff reds.
- [x] 1.5 Run E1 and E5 locally (orchestrator: node-27 temporary worktree half of E5).
- [x] 1.6 E6: `openspec validate partition-oversized-reconcile-suites --strict --no-interactive`.
