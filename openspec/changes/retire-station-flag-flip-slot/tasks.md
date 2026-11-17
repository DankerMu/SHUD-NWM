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

- [ ] 1.1 Record the pre-split collected node ids of `tests/test_variant_activation_cutover.py`.
- [ ] 1.2 Split the file per proposal What 1. Move the harness to `tests/variant_activation_cutover_helpers.py`, and keep
  the base path as a module-scope importer of `packages.common.model_registry`.
- [ ] 1.3 Register the partitions and the helper module in every rule, table, pin and reference listed in What 1. Add
  red-before-green selector pins for each new partition and the helper module.
- [ ] 1.4 Drop `station_flag_flip` from the tuple. Rewrite the tests that register on it per What 2, add the
  unknown-mount case, and update the comments.
- [ ] 1.5 Evidence: node-count parity (E2), mutation red for ordering and abort-on-raise, unknown-mount red on the old
  tuple, every file under 1000 lines.
- [ ] 1.6 File the state-clone wiring follow-up issue (What 5).
