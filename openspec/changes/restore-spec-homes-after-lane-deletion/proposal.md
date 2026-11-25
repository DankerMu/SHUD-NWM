# Give three live behaviours a spec home again, and correct two environment notes

## Why

The lane deletion of 2026-11-19 (#3221) and its review removed requirements
whose only producer was a deleted lane. Three live behaviours lost their
spec text in the process, or kept text that describes the deleted producer
(#3233, items added in the issue's comments):

- **The live monitor.** `services/orchestrator/monitoring.py` raises five
  alerts and links three runbooks; `tests/test_live_monitoring.py` covers
  three of the five (`slurm_monitor_unavailable` and
  `scheduler_evidence_missing` are not exercised). No requirement names it
  since "Monitoring and alerts cover closure risks" was removed.
- **Non-finite SHUD output.** The output parser refuses `NaN` / `inf` flow
  values with `NON_FINITE_FLOW` and a test proves it; the scenario lived in
  the retired `staging-end-to-end-closure` capability. `output-parsing`
  covers only non-numeric values and column-count mismatch.
- **Deferred auth.** The scenario "Deferred auth is a release blocker" asks
  for "a release-blocker artifact" that only a deleted lane produced. What
  the backend does is answer each affected HTTP request with 503
  `RELEASE_BLOCKED` (the same response `rbac-policy-enforcement` already
  describes as "Release blocked dependency 503"). The CLI path reaches the
  same policy decision but raises its own error, without a 503; the
  scenario is scoped to HTTP. A request that a dependency-guarded route
  refuses for its own shape (a malformed body, an invalid run id, a
  disabled route) gets that refusal first; the scenario is scoped to
  well-formed requests to enabled routes.

Two notes about environment files are wrong or misleading:

- `infra/env/node27-download.example` says "the download chain opens no
  database connection today". The download entry point requires a DSN,
  and the GFS and IFS adapters are both built with `PsycopgMetStore`
  from it: they write `met.data_source` and read and write
  `met.forecast_cycle`. `docs/runbooks/production-ops/artifacts-and-storage.md`
  repeats the claim in its role table.
- On the compute host, `compute.host.env` (read by the gateway and the
  compute API) and `compute.replay.env` still carry
  `NHMS_SCHEDULER_CYCLE_LAG_HOURS=16` and `NHMS_SCHEDULER_LOOKBACK_HOURS`
  of 48 / 288, from before the scheduler's own env file moved to 10 / 32.
  Only the orchestrator CLI, the journal retention and the state-index
  scripts read those two keys, and none of them loads those two files, so
  the values have no effect; they mislead a reader. (The gateway does read
  other `NHMS_SCHEDULER_*` keys from `compute.host.env`; only the lag and
  lookback keys are inert.)

One more sentence of the same kind as those cut in the first PR remains in
`docs/runbooks/slurm-gateway-preflight.md` ("Final readiness still requires
accepted Slurm accounting receipts ...").

## What changes

- `production-ops-readiness`: one requirement added (the monitor's five
  alerts, their payload and their runbooks); the scenario about deferred
  auth is replaced by what the backend does.
- `output-parsing`: one requirement added (non-finite values).
- Tests: the monitor's five alerts row by row (two are untested today),
  the mutual exclusions and the "every linked runbook exists" check; the
  503 `RELEASE_BLOCKED` response details on the gateway side, where only
  the status and code are asserted today; a non-finite value after valid
  rows. No production code changes are
  expected; if a test shows the code does not do what the spec text says,
  the implementer stops and reports instead of changing behaviour.
- `infra/env/node27-download.example`: the comment says what the chain
  does with the database.
- `infra/env/README.md`: a note that the scheduler's lag and lookback keys
  in `compute.host.env` and `compute.replay.env` are inert, which file the
  scheduler reads them from, and that those two keys should be dropped at
  the next edit of those host files.
- `docs/runbooks/slurm-gateway-preflight.md`: the stale sentence is cut.

## Decisions

- **D1 Spec the monitor as it is.** The requirement is a description of
  the five alerts the code raises today, not a redesign. Whether
  `source-latency.md` is the right runbook for the two scheduler alerts
  stays open; the requirement only demands that a linked runbook exists.
- **D2 The deferred-auth scenario keeps its name and gets a true body.**
  The behaviour exists and protects production actions. The scenario's
  WHEN / THEN are rewritten to the per-request 503; its title stays, so
  the requirement's scenario set is unchanged.
- **D3 The host env files are not edited by this change.** They are
  untracked files on the production host; the stale keys are inert. The
  README records the fact and the follow-up.
- **D4 The download template's comment follows the code.** The issue asked
  which of the two is the intent. The chain has written `met.data_source`
  and `met.forecast_cycle` through that role since it was provisioned, the
  role holds DML on `met` for exactly that, and the display side reads
  those rows; the comment is what is wrong.

## What does not change

- `services/orchestrator/monitoring.py`, `workers/output_parser/parser.py`,
  `apps/api/auth.py`, `packages/common/auth_policy.py`: no behaviour change.
- The role, DSN and endpoint allowlist of the download template.
- Any env file on any host.

## Impact

- Specs: `production-ops-readiness` (one added, one modified),
  `output-parsing` (one added).
- Tests: `tests/test_live_monitoring.py`, the API auth test file that owns
  release-blocked responses.
- Docs / templates: `infra/env/node27-download.example`,
  `infra/env/README.md`, `docs/runbooks/slurm-gateway-preflight.md`.
