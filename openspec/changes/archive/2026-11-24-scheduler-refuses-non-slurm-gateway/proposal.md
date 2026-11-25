# The scheduler refuses a gateway that is not on the slurm backend

## Why

#3246 (`docs/bugs.md` BUG-20260527-013). Two defects:

1. `SlurmGatewaySettings.backend` defaults to `mock`. A deployed gateway is on
   the real backend only because its environment says
   `SLURM_GATEWAY_BACKEND=slurm`. If that line is lost the gateway starts
   healthy on the mock backend and answers submissions with made-up Slurm job
   ids. The scheduler's gateway probe reads the gateway's health body, which
   already names the backend, and ignores the name: a remote gateway reporting
   `mock` with every binary present passes as healthy and submit-capable.
2. The backend name has two vocabularies. `create_gateway` accepts `mock` and
   `slurm` and raises on anything else; the scheduler's probe and its
   self-reference check also treat `real` as a real backend, and the tests
   use `real` in four ways (three environment settings, one argument, three
   patched backend readers and one shared helper with six callers; injected
   probe results also carry it as `mode`). `scheduler_cohort_split` compares with `slurm` only. A scheduler
   configured with `real` therefore probes over HTTP but never splits cohorts,
   and a gateway configured with `real` cannot serve a request.

## What Changes

- The scheduler's HTTP gateway probe fails closed when the health body names
  a backend other than `slurm`: the gateway check is unhealthy and not
  submit-capable, the pass gets the existing `SLURM_GATEWAY_UNAVAILABLE`
  blocker with a reason that names the reported backend, and the check
  records the reported backend. A health body without a `backend` key is
  judged as today.
- One vocabulary, `mock` and `slurm`. `real` is removed from the scheduler's
  two comparisons and from the tests and the runbook lines that use it. A
  scheduler configured with `real` (or any other unknown name) takes the
  in-process path, where `create_gateway` rejects the name, so its gateway
  check is unhealthy.
- `slurm-gateway-node22-deployment`: one requirement added.
- `docs/bugs.md` BUG-20260527-013 records the outcome.

## Decisions

- **Guard at the scheduler's probe, not at gateway start-up.** The issue
  offered both. The start-up refusal needs a marker that tells a deployed
  gateway from a development one, and none reaches the process reliably: the
  deployed unit declares a gateway role in an `Environment=` line, but the
  environment file loaded by the same unit sets a different role and wins, and
  the generic unit template declares no role at all. A start-up guard would
  also stay inert until the long-running gateway is next restarted. The probe
  runs at the start of every pass, sits exactly where made-up job ids would
  enter the journal (the scheduler is the only HTTP client that submits), and
  needs no environment change and no gateway restart.
- **The check is for the HTTP probe only.** `_interpret_gateway_health` is
  shared with the in-process path used by development and tests on the mock
  backend. The required backend is passed in by the HTTP branch; the
  in-process branch passes nothing and behaves as today.
- **Present-and-different blocks; absent does not.** The function already
  tolerates an older health shape. The current gateway always sends the key
  (`SlurmHealthResponse.backend` is a required field and the real backend
  sets `slurm`), so tolerance costs nothing today and avoids fencing on a
  body the code cannot interpret.
- **Remove `real`, do not alias it.** No gateway can run under that name, so
  nothing working depends on it. An alias would need a third meaning in
  `scheduler_cohort_split`.
- **Same blocker code.** A gateway on the wrong backend is one more way the
  gateway cannot be used for submission; operators and the stall-health
  script already key on `SLURM_GATEWAY_UNAVAILABLE`. The reason text and the
  recorded backend tell the cases apart.
- **The comparison reads the payload, not the fallback.** The function
  reports `backend` as the payload's value or, when the key is absent, the
  scheduler's own mode. The guard compares the payload's own value only, so
  an absent key can never satisfy or fail it by way of the fallback.
- **The production scheduler already runs with `slurm`.** The gateway check
  recorded by the last submitting pass before this change
  (`scheduler_2026112422_477019b84333`) has mode `slurm`, backend `slurm`,
  healthy and submit-capable. Removing `real` and requiring `slurm` cannot
  block that deployment. Idle passes do not record a gateway check.

## Known limits

- A scheduler whose own environment loses `SLURM_GATEWAY_BACKEND` takes the
  in-process path and never asks the remote gateway. Its submissions still go
  to the gateway URL over HTTP; what it loses is the HTTP probe and the cohort
  split. Not covered here.
- The gateway still starts on the mock backend when its environment says
  nothing. After this change the scheduler will not submit through it.
- The control API can mount the gateway routes in-process on the mock backend
  in development; that path is unchanged.

## Impact

- Code: `services/orchestrator/scheduler_preflight.py`,
  `services/orchestrator/scheduler_gateway.py`.
- Tests: `tests/test_production_scheduler.py`,
  `tests/test_slurm_gateway_auth_deployment.py`.
- Docs: `docs/runbooks/slurm-gateway-preflight.md`, `docs/bugs.md`. The bug
  entry is updated in this change rather than after the merge, so that the
  final push is complete; it records the code fix, and the compute-node
  evidence is posted on the issue.
- Deploy: an ordinary fast-forward at a pass boundary; the gateway process is
  not restarted.
