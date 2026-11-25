# Tasks

Fixture level: `standard`. Risk packs: production pass gating (a false
refusal blocks every pass), compatibility of the development and test default.

Must preserve: with no backend configured, the in-process mock probe is
healthy and submit-capable and no test needs extra environment;
`tests/test_gateway.py` (default mock through the full app) unchanged and
green; every existing test whose fake health body reports `backend: "slurm"`
or no backend unchanged in outcome (the one existing test whose fake body
reports `mock` over HTTP is named in 2.8); the self-reference blocker still fires
for the `slurm` backend on a loopback URL at the service port; the blocker
code `SLURM_GATEWAY_UNAVAILABLE`, its `field`, `host` and `port`; the keys of
the gateway check (`mode`, `backend`, `version`, `healthy`, `submit_capable`,
`accounting_available`, `endpoint`, `self_reference`); `create_gateway`,
`SlurmGatewaySettings` and the gateway service are not edited.

## 1. Code

- [x] 1.1 `_interpret_gateway_health` (`services/orchestrator/scheduler_preflight.py`):
  new keyword-only parameter `required_backend: str | None = None`. When it is
  given and the payload carries a `backend` key whose value, as a stripped
  string, is not equal to it, the verdict is unhealthy (`healthy`,
  `submit_capable` and `accounting_available` false) and the reason list gains
  a sentence that names the reported backend and the required one. The
  returned `backend` stays the reported value. With the parameter absent, or
  the key absent from the payload, the function behaves exactly as today.
  The comparison reads the payload's own `backend` value, never the
  `mode` fallback used for the returned field. Update the docstring.
- [x] 1.2 `_default_gateway_probe` (`services/orchestrator/scheduler_gateway.py`):
  the HTTP branch passes `required_backend="slurm"`. The branch condition
  becomes "mode is `slurm`" (`real` removed). The in-process call is not
  changed.
- [x] 1.3 `_gateway_self_reference_blocker`: the real-backend test becomes
  "backend is `slurm`" (`real` removed). Docstring wording follows.
- [x] 1.4 Search the repository (code, infra, docs outside dated receipts and
  archives) for other uses of `real` as a backend name and report them. Fix
  `docs/runbooks/slurm-gateway-preflight.md`: every place that names `real`
  as a backend value (three at the time of writing), and add one sentence on
  the new refusal where the runbook describes what the probe checks.
- [x] 1.5 `docs/bugs.md` BUG-20260527-013: `status: resolved` with a
  `resolved_by` field in the form the other resolved entries use, naming
  #3246 and what the scheduler does now; the prose states the chosen guard
  and the known limits from the proposal. `scripts/validate_bugs_ledger.py`
  passes.

## 2. Tests

- [x] 2.1 `_interpret_gateway_health` unit rows. No test calls the function
  directly today; put the new tests in `tests/test_production_scheduler.py`
  next to the `_install_fake_httpx` helper and the HTTP-probe tests:
  with `required_backend="slurm"` — backend `mock` healthy body -> unhealthy,
  reason names `mock`, returned backend `mock`; backend `slurm` -> healthy;
  no backend key -> healthy; backend `mock` and a missing binary -> unhealthy
  with both reasons; backend with surrounding whitespace or different case
  (`" slurm "`, `"SLURM"`) -> state and pin what happens (whitespace is
  stripped, case is not folded: `SLURM` is refused, because the gateway only
  ever reports the lower-case name); no backend key with `mode="mock"` and
  `required_backend="slurm"` -> healthy (the comparison does not read the
  fallback). Without `required_backend` — backend `mock` healthy body ->
  healthy (the in-process path).
- [x] 2.2 Probe level, with the fake `httpx` helper the scheduler tests
  already use: scheduler backend `slurm`, URL and token configured, health
  body healthy with four executable binaries and `backend: "mock"` ->
  `_default_gateway_probe` answers unhealthy, not submit-capable.
- [x] 2.3 Check level (`_slurm_gateway_check` with the default probe and the
  same fake): `checks["backend"] == "mock"`, `checks["healthy"] is False`,
  one blocker with code `SLURM_GATEWAY_UNAVAILABLE` whose `reason` contains
  `mock`. With `backend: "slurm"`: no blocker. With no `backend` key: no
  blocker.
- [x] 2.4 Pass level. No existing test drives a whole pass that is blocked
  by `SLURM_GATEWAY_UNAVAILABLE`. Reuse the `run_once`-level setup of the
  tests built on `_slurm_shud_scheduler` in `tests/test_production_scheduler.py`
  (they stub the gateway probe as healthy), without the probe stub and with
  the fake `httpx` reporting `mock`: the pass is blocked, nothing is
  submitted, and the pass evidence carries the blocker with the reason. If
  that setup cannot run without the stub for a reason unrelated to this
  change, say so and pin the `_slurm_preflight` aggregation level instead.
- [x] 2.5 Scheduler backend `real`: the fake `httpx` records no request, the
  gateway check is unhealthy with `SLURM_GATEWAY_UNAVAILABLE`, and the reason
  comes from `create_gateway` rejecting the name.
- [x] 2.6 Self-reference: backend `slurm`, loopback URL at the service port
  -> blocker (existing behaviour; the parametrized test that passed
  `backend="real"` now passes `slurm`). Backend `real` with the same URL ->
  no self-reference blocker (it is not a real backend any more; 2.5 shows the
  check fails anyway).
- [x] 2.7 Development default (new; nothing pins it today): backend unset
  -> `_slurm_gateway_check` with the default probe is healthy through the
  in-process mock gateway, `checks["backend"] == "mock"`, no blocker, and the
  fake `httpx` records no request.
- [x] 2.8 Remove `real` as a backend name from the tests. Record each
  site and its outcome before editing; after the edit every one of these
  tests passes with unchanged assertions, the two exceptions below aside:
  - environment settings in `tests/test_slurm_gateway_auth_deployment.py`
    (three tests) and in the `_install_fake_httpx` helper of
    `tests/test_production_scheduler.py` (six callers) -> `slurm`;
  - the `backend="real"` argument of the parametrized self-reference test ->
    `slurm`;
  - the three patched backend readers that return `"real"` -> `"slurm"`;
  - injected probe results that carry `"mode": "real"` -> `"slurm"`, and the
    one assertion that echoes that injected value follows (exception 1: its
    literal changes, nothing else);
  - `test_preflight_http_probe_legacy_status_only_fallback` sends
    `{"status": "ok", "backend": "mock"}` over the HTTP branch and asserts
    healthy. That body is not the legacy shape it means to test (the legacy
    gateway sent no backend name) and would now be refused. Its body becomes
    `{"status": "ok"}` (exception 2); its assertions stay.
  None of these tests reaches the cohort split. If one changes behaviour for
  another reason, stop and report it with the failing assertion.

## 3. Evidence

- [x] 3.1 Red first: 2.1 (rows with `required_backend`), 2.2, 2.3 (`mock`
  case), 2.4, 2.5 and the second half of 2.6 fail on the unmodified source;
  2.3 (`slurm` and absent cases), 2.6 first half and 2.7 pass before and
  after. Output saved to
  `.workplans/3246/red-runs.log`.
- [x] 3.2 Mutations, each applied by hand, checked, recorded, restored
  (`cmp` against a scratch copy):
  - M1: remove the backend comparison in `_interpret_gateway_health` -> 2.1,
    2.2, 2.3 red.
  - M2: the HTTP branch stops passing `required_backend` -> 2.2, 2.3 red,
    2.1 green.
  - M3: compare the fallback value instead of the payload's own value ->
    the 2.1 row with no backend key and `mode="mock"` red; treat an absent
    key as a mismatch -> the "no backend key" rows of 2.1 and 2.3 and the
    legacy-shape test of 2.8 red.
  - M4: apply the comparison without the parameter (always require `slurm`)
    -> the in-process rows of 2.1 and 2.7 red.
  - M5: put `real` back into the probe's branch condition -> 2.5 red.
  - M6: put `real` back into the self-reference test -> 2.6 second half red.
- [x] 3.3 `uv run --no-sync ruff check --no-cache .` clean. Test files:
  `tests/test_slurm_gateway_auth_deployment.py`, `tests/test_gateway.py`,
  every `tests/test_slurm_gateway*.py`, the scheduler test files that
  reference the gateway probe or the self-reference blocker
  (`git grep -l "_default_gateway_probe\|_interpret_gateway_health\|_gateway_self_reference_blocker\|_slurm_gateway_check" -- tests`),
  `tests/test_facade_name_resolution.py` and
  `tests/test_scheduler_candidate_runtime_identity.py` (the function is
  re-exported through the facade). `tests/test_production_scheduler.py` is
  very large: targeted `-k` selections while iterating, the whole file once at
  the end.
- [x] 3.4 node-27: the files of 3.3 in a detached temporary worktree.
  - Evidence: `e140f5dbb`, 2397 passed, no skips; re-run at `b4b6a14a9` (receipt on the PR).
- [x] 3.5 CI: full suite.
## 4. After merge (evidence owed on the issue, not a task of this change)

Compute node, taken by the orchestrator after merge with an
  ordinary fast-forward at a pass boundary and no gateway restart. Idle
  passes do not record a gateway check, so the evidence is: the first idle
  pass on the new code ends `planned` with root preflight `ready`; the next
  submitting pass records the gateway check with mode `slurm`, backend
  `slurm`, healthy and submit-capable, and submits. The gateway unit's start
  time is unchanged across the deploy. The issue is closed with that
  evidence, not by the merge.

## Review (round 1 not clean, round 2 clean)

- Round 1, test-evidence seat: the pass-level test of 2.4 ran on a setup that
  was blocked before the change for another reason, so it did not show a
  submit being stopped. Added
  `test_submitting_pass_stops_only_when_remote_gateway_reports_non_slurm_backend`
  on the setup that submits: gateway reports `slurm` -> submitted; reports
  `mock` -> nothing submitted, `SLURM_GATEWAY_UNAVAILABLE`. On the pre-change
  source the `mock` case fails because the pass submits.
- The mutation log predated two small follow-up edits (the conditions written
  as `!= "slurm"`, a `backend: null` row); M1, M2, M5 and M6 were re-run on
  the final tree with assertion lines. M3 and M4 were not re-run after those
  edits; the edits do not touch the code they mutate.
- A body with `backend: null` is refused, and the recorded backend then shows
  the scheduler's mode (the fallback), not the null. Pinned by a test; the
  real gateway never sends null.
- The compute-node evidence of section 4 is collected after the merge; the
  issue is closed with it.
