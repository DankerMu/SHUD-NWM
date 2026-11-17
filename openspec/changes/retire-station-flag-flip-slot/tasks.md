# Tasks — retire-station-flag-flip-slot (#3165)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Legacy compatibility / examples | yes | test split keeps every node and assertion: E2 |
| CI routing / test selection | yes | partitions and helper registered in rules, tables, pins: E3 selector pins |
| Public API / CLI / script entry | yes | `PRE_ACTIVATION_HOOK_MOUNT_POINTS` and `register_pre_activation_hook` rejection: E3 |
| Error handling / rollback / partial outputs | yes | abort-on-raise and rollback coverage kept: E3 mutation |
| Documentation / migration notes | yes | two spec deltas, code comments: E4 |
| Others | no | no DB, IO, config, concurrency or runtime change |

- [x] 1.1 Record the pre-split collected node ids and per-definition fingerprints of
  `tests/test_variant_activation_cutover.py`.
- [x] 1.2 Pure-move commit: split the file per proposal What 1 (helper module including `_ManifestPublisherStub`;
  fixture imports with `pyproject.toml` per-file F401/F811 ignores; BLE001 moved to the dispatch partition). Register
  the partitions and the helper module in every rule, table, pin and reference listed in What 1, with red-before-green
  selector pins and the tracked-tree guard. Prove E2 on this commit.
- [x] 1.3 Rewrite commit: drop `station_flag_flip` from the tuple. Rewrite the tests that register on it per What 2
  (two-entry local tuple for the ordering and abort cases), add the unknown-mount case, and update the comments.
- [x] 1.4 Evidence: the E3 mutations, the selector pins and the tracked-tree guard red, and every file within the E1
  line rule.
- [x] 1.5 File the follow-up issue: state-clone wiring (What 5) and the stale "Change 8 owns the flip" comments.
