# Tasks

## 1. Implementation

- [x] 1.1 Runbook: both guards classify the printed state; comment updated.
- [x] 1.2 Gateway contract test: new pin, order, no `--quiet`, state lists equal to the Python constants,
      extracted guard executed against a fake `systemctl` over all states; file stays at or under 1000 lines.
- [x] 1.3 Selector comment at `scripts/select_ci_tests.py:5749-5751` (list A).
- [x] 1.4 `file-provider-refresh.md` direct wrapper runs (list B), without growing the file.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; `tests/test_slurm_gateway_deployment_contract.py`; the suites that
      read `file-provider-refresh.md` and `gateway-and-services.md`; selector meta-guards after staging;
      markdown lint of the two runbooks; `openspec validate gateway-rollout-pass-guard --strict --no-interactive`.
- [ ] 2.2 CI green on the PR.
- [x] 2.3 node-22 read-only reading during a pass (2026-10-08 08:03Z, in the proposal).
      The new rollout guard block alone, piped to node-22's bash 5.2.21 at 08:30Z while a pass was
      `activating` (query only): printed the two instruction lines with the state and exited 1.

Deviation: the behaviour of `systemctl --user start` on the already-starting scheduler service and the
`ConditionResult` it leaves were not exercised on node-22 (that would be a start on the production
scheduler); the proposal's account of today's failure shape is an inference. Whether a `failed` service
stays `failed` after a condition-skipped start was not tested either. No node-27 path is touched.
