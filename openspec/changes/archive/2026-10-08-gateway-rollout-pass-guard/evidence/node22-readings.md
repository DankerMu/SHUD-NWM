# node-22 read-only readings (#2779, PR #2783)

Host node-22, 2026-10-08, queries only; no rollout or rollback step was executed.

- 08:03:39Z, during a scheduler pass: `systemctl --user is-active nhms-compute-scheduler.service` printed
  `activating`, exit 3; with `--quiet`, exit 3; `show`: `ActiveState=activating SubState=start ConditionResult=yes`.
- 08:30:19Z, service `activating`: the new rollout guard block (capture line to `esac`, with `set -euo pipefail`
  prepended) piped to `bash -s` (GNU bash 5.2.21): stderr
  `rollout: a scheduler pass is still running (activating); let it finish naturally` and
  `rollout: re-run this rollout step once nhms-compute-scheduler.service is inactive`; exit 1.

Not exercised: `systemctl --user start` on the already-starting scheduler service, the `ConditionResult` it
leaves, and whether a `failed` service stays `failed` after a condition-skipped start.

CI (PR #2783, `c2e962c40`): Unit Tests, Markdown Lint, Production Topology Hard Gate pass. Review: 1 round, no
P0/P1; one P2 (selector routing for the new import) fixed in `c2e962c40`, not re-reviewed.
