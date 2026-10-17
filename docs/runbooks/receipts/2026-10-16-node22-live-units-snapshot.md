# node-22 live user-scope units snapshot (read-only, 2026-10-16)

Read-only `ssh ... cat` of the deployed node-22 user systemd units on 2026-10-16 (node-22 HEAD 7ac79243, booted 2026-10-13 16:20 CST).
Purpose: byte-exact source for `infra/systemd/node22/` (change `node22-config-hygiene`, task 1.1). Secrets appear only as file paths.
`%h` on node-22 expands to `/users/frd_muziyao/`, and `/users/frd_muziyao/NWM` does not exist; the checkout is `/scratch/frd_muziyao/NWM`.

| relative path under `~/.config/systemd/user/` | sha256 | mtime |
|---|---|---|
| `nhms-compute-api.service` | `c6dff9aa650bb0741401e1da0cdd0120cc66a403373d8803b214d322b7469ab7` | 2026-06-12 18:47:07 |
| `nhms-compute-scheduler.service` | `caacdab45a4bfe1b2b8c0ff0b1dd27e0fef3eb33e5913a14964640800847dbc5` | 2026-06-29 04:57:34 |
| `nhms-compute-scheduler.timer` | `858ab2f357a1116ba765dc78aa328b5c16c815778cd0f08c34c7bbdcb4fbfeaa` | 2026-06-08 00:01:26 |
| `nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf` | `9ebc7fdd3c9e05fd692d50b12436c5ff2c35a218825d6b8d6f2601e769e6e7a7` | 2026-09-01 22:15:15 |
| `nhms-compute-scheduler.service.d/20-runtime-path.conf` | `ae59e26c2440c3a9e886c1f2914d6f4907c67f881feb5ca49d7d1555b1d66a97` | 2026-08-30 18:57:53 |
| `nhms-slurm-gateway.service.d/10-node22-live.conf` | `de28aa310e7ba456578f07d558a7fb932e6cd0ff30f4b4ac561e924e40e4ef2f` | 2026-09-01 22:37:08 |
| `nhms-slurm-gateway.service` | `56a54b2eff37d52002ecbb6b6dd02f143b3ad61941e9fd1971f7366a270b6afb` | 2026-06-06 19:12:06 |

## `nhms-compute-api.service`

sha256 `c6dff9aa650bb0741401e1da0cdd0120cc66a403373d8803b214d322b7469ab7`

```ini
[Unit]
Description=NHMS compute/control API (node 22 user)
After=network-online.target nhms-slurm-gateway.service
Wants=network-online.target nhms-slurm-gateway.service

[Service]
Type=simple
WorkingDirectory=/scratch/frd_muziyao/NWM
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.host.env
Environment=NHMS_SERVICE_ROLE=compute_control
Environment=SLURM_GATEWAY_URL=http://127.0.0.1:8090
Environment=NHMS_PRODUCTION_SLURM_ENABLED=true
ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8001 --log-level info
StandardOutput=append:/scratch/frd_muziyao/nhms-prod/runtime/api/nhms-api-8001.log
StandardError=append:/scratch/frd_muziyao/nhms-prod/runtime/api/nhms-api-8001.log
Restart=on-failure
RestartSec=5
TimeoutStartSec=0
TimeoutStopSec=30
LimitNOFILE=65536

[Install]
WantedBy=default.target
```

## `nhms-compute-scheduler.service`

sha256 `caacdab45a4bfe1b2b8c0ff0b1dd27e0fef3eb33e5913a14964640800847dbc5`

```ini
[Unit]
Description=NHMS production scheduler one bounded continuous pass (node 22 user)
After=nhms-slurm-gateway.service
Wants=nhms-slurm-gateway.service

[Service]
Type=oneshot
WorkingDirectory=/scratch/frd_muziyao/NWM
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
Environment=NHMS_SERVICE_ROLE=compute_control
Environment=SLURM_GATEWAY_URL=http://127.0.0.1:8090
Environment=NHMS_PRODUCTION_SLURM_ENABLED=true
ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --submit --continuous --max-passes 1
StandardOutput=append:/scratch/frd_muziyao/nhms-prod/workspace/scheduler/logs/nhms-compute-scheduler.log
StandardError=append:/scratch/frd_muziyao/nhms-prod/workspace/scheduler/logs/nhms-compute-scheduler.err
TimeoutStartSec=0
LimitNOFILE=65536
```

## `nhms-compute-scheduler.timer`

sha256 `858ab2f357a1116ba765dc78aa328b5c16c815778cd0f08c34c7bbdcb4fbfeaa`

```ini
[Unit]
Description=Run NHMS production scheduler every five minutes

[Timer]
OnBootSec=2min
OnActiveSec=30s
OnUnitActiveSec=5min
Persistent=false
Unit=nhms-compute-scheduler.service

[Install]
WantedBy=timers.target
```

## `nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`

sha256 `9ebc7fdd3c9e05fd692d50b12436c5ff2c35a218825d6b8d6f2601e769e6e7a7`

```ini
[Service]
EnvironmentFile=
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
EnvironmentFile=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
```

## `nhms-compute-scheduler.service.d/20-runtime-path.conf`

sha256 `ae59e26c2440c3a9e886c1f2914d6f4907c67f881feb5ca49d7d1555b1d66a97`

```ini
[Service]
Environment=NHMS_PYTHON_VENV_BIN=/scratch/frd_muziyao/NWM/.venv/bin
```

## `nhms-slurm-gateway.service.d/10-node22-live.conf`

sha256 `de28aa310e7ba456578f07d558a7fb932e6cd0ff30f4b4ac561e924e40e4ef2f`

```ini
[Service]
EnvironmentFile=
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.host.env
EnvironmentFile=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
Environment=SLURM_GATEWAY_URL=http://127.0.0.1:8090
Environment=SLURM_GATEWAY_SLURM_BIN_PATH=/usr/bin
WorkingDirectory=/scratch/frd_muziyao/NWM
ExecStart=
ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway
```

## `nhms-slurm-gateway.service`

sha256 `56a54b2eff37d52002ecbb6b6dd02f143b3ad61941e9fd1971f7366a270b6afb`

```ini
[Unit]
Description=NHMS standalone Slurm gateway (node 22 user)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/scratch/frd_muziyao/NWM
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.host.env
Environment=NHMS_SERVICE_ROLE=slurm_gateway
Environment=SLURM_GATEWAY_BACKEND=slurm
Environment=SLURM_GATEWAY_URL=http://127.0.0.1:8090
Environment=SLURM_GATEWAY_TEMPLATE_DIR=infra/sbatch
Environment=SLURM_GATEWAY_RESOURCE_PROFILES_PATH=config/resource_profiles.yaml
Environment=SLURM_GATEWAY_PARTITION_OVERRIDE=CPU
Environment=SLURM_GATEWAY_SLURM_BIN_PATH=/usr/bin
ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway
Restart=on-failure
RestartSec=5
TimeoutStartSec=0
TimeoutStopSec=30

[Install]
WantedBy=default.target
```
