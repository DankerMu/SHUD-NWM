# Tasks — playwright-ubuntu-2604-support (#3198)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Legacy compatibility / examples | yes | m15 replays historical SHAs, so its pin stays: What 3, What 5, E5 |
| CI / environment | yes | runner OS and node-27 browser cache: E3, E4, E6 |
| Others | no | no runtime code change |

- [x] 1.1 Upgrade `@playwright/test` to `^1.64.1` (lockfile 1.64.1) and update the lockfile (What 1).
- [x] 1.2 Unpin `ci.yml` `frontend-e2e`; rewrite the m15 pin comment; add the runner-image guard to `tests/test_ci_shard_tests.py` (What 2, What 3, What 5).
- [x] 1.3 E1 and E2 locally.
- [ ] 1.4 E3, E4 and E5 (orchestrator).
- [ ] 1.5 E6 after merge (orchestrator), recorded in the archive PR.
