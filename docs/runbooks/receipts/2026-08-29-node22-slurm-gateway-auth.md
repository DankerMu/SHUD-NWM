# Node-22 Slurm Gateway Mutation Auth Receipt

Captured: 2026-08-29

Scope: issue #1684, PR #1888, OpenSpec tasks 3.4 / 4.7.

Final deployed source:

```text
worktree=/scratch/frd_muziyao/NWM-issue-1684
head=de63a5ad3d6cc726e2e1393141f60b6538a78b59
python=/scratch/frd_muziyao/NWM/.venv/bin/python (existing active Python 3.12.7 environment; no uv sync)
gateway_url=http://127.0.0.1:8090
slurm_bin_path=/usr/bin
```

No credential value, Authorization header, signed URL, environment value, or business payload is present in this receipt.

## Coordinated Maintenance Fence

The scheduler timer and service were fenced with reboot-ephemeral runtime
`ConditionPathExists` drop-ins while the gateway was switched. A current
scheduler pass was allowed to finish naturally; the service was not stopped or
killed. Direct start probes were condition-skipped and remained inactive with
`ConditionResult=no`.

After verification, both runtime fence drop-ins were removed, systemd was
reloaded, and the timer was resumed. Final state:

```text
runtime_condition_fence=absent
nhms-compute-scheduler.timer=active
nhms-slurm-gateway.service=active
```

## Credential and Effective Unit Configuration

Gateway and scheduler consume the same untracked secret file. Only its path and
metadata were inspected:

```text
/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env mode=0600 owner=frd_muziyao
gateway drop-in mode=0600 owner=frd_muziyao
scheduler drop-in mode=0600 owner=frd_muziyao
gateway EnvironmentFiles include compute.host.env + shared secret
scheduler EnvironmentFiles include compute.scheduler-dbfree.env + shared secret
gateway WorkingDirectory=/scratch/frd_muziyao/NWM-issue-1684
```

The gateway uses the checked-in module entrypoint with deterministic h11 and
real Slurm binaries. No dependency installation or environment rebuild was
performed.

## Local Auth Boundary and Zero-Side-Effect Proof

During the auth-boundary probes, `sbatch` and `scancel` were temporarily routed
through receipt-only blockers: `--version` delegated to the real binaries, but
any mutation attempt would create a fixed marker and exit before reaching
Slurm. The marker remained absent. The gateway was then restored to real
`/usr/bin` Slurm binaries before scheduler continuity verification.

Observed HTTP results:

```text
POST /api/v1/slurm/jobs, no credential -> 401 AUTH_REQUIRED
POST /api/v1/slurm/jobs, wrong credential -> 401 AUTH_REQUIRED
POST /api/v1/slurm/job-arrays, no credential -> 401 AUTH_REQUIRED
DELETE /api/v1/slurm/jobs/12345, no credential -> 401 AUTH_REQUIRED
DELETE /api/v1/slurm/jobs/12345, wrong credential -> 401 AUTH_REQUIRED
POST /api/v1/slurm/jobs, configured credential + invalid body -> 422
POST /api/v1/slurm/job-arrays, configured credential + invalid body -> 422
POST /api/v1/slurm/internal/reset, no credential -> 404
POST /api/v1/slurm/internal/reset, configured credential -> 404
```

Each checked 401 response carried the canonical deny decision and an audit
record with `no_mutation_expected=true`. The blocker marker proved zero
`sbatch` and zero `scancel` calls. The configured-credential 422 probes failed
at request validation and did not reach a Slurm mutation. Reset was absent from
the route inventory, so neither reset probe could mutate registry state.

## Loopback and Remote-Reachability Boundary

Node-22 listener evidence:

```text
127.0.0.1:8090 LISTEN
0.0.0.0:8090 absent
[::]:8090 absent
```

The checked-in module entrypoint was invoked with
`--url http://0.0.0.0:8090` and exited with code 2 while reporting that a
non-loopback bind host is not allowed. The running gateway remained healthy,
showing rejection occurred before a second uvicorn socket opened.

A probe from the local Mac established the discrimination boundary:

```text
node-22 TCP/32099 (SSH control) -> reachable
node-22 TCP/8090 (gateway) -> unreachable
remote_refusal=PASS
```

No host packet-filter or ACL receipt is claimed; the accepted equivalent
controls are the entrypoint bind guard plus the remote negative probe.

## Real Slurm Health and Scheduler Continuity

After the blocker proof, the live gateway was switched back to real `/usr/bin`
Slurm binaries. Read-only health reported:

```text
backend=slurm
healthy=true
sbatch resolved=true executable=true
squeue resolved=true executable=true
sacct resolved=true executable=true
scancel resolved=true executable=true
```

The scheduler-owned read-only gateway probe reported all three required flags:

```text
healthy=true
submit_capable=true
accounting_available=true
```

At least one timer-triggered scheduler pass completed after the final-SHA
gateway became active:

```text
gateway final active since=2026-08-29 19:58:21 CST
completed scheduler pass=2026-08-29 20:58:09 CST
Result=success
ExecMainStatus=0
```

Subsequent timer ticks may already be running or complete; they are normal
business operation and are not part of the receipt wait condition.

## Verdict

PASS for OpenSpec tasks 3.4 / 4.7:

- final SHA deployed on node-22;
- shared owner-only credential active without disclosure;
- mutation denials and pre-validation passage match the contract;
- zero Slurm/reset side effect proved for receipt probes;
- gateway is loopback-only and remotely unreachable;
- deliberate non-loopback start is rejected before uvicorn;
- real Slurm health and scheduler preflight are healthy;
- scheduler timer is restored to business operation and a final-SHA pass
  completed successfully.
