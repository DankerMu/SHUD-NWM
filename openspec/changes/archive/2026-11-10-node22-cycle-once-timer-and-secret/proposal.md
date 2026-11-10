# Proposal: node22-run-cycle-once.sh 与 scheduler timer 串行，并加载 gateway secret（#3113）

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent（expanded：触及 CLI/脚本入口、生产 systemd 状态、secret 加载、与 timer 的并发）
Blast radius:
- timer 停掉后若没恢复，scheduler 会永久停摆，追赶冻结；
- secret 若泄漏进日志，就是凭据外泄；
- 若破坏现有参数或环境语义，exact-cycle 修复流程失效。
Selected risk packs: Public API / CLI / script entry；Auth / permissions / secrets；Concurrency / shared state / ordering；Error handling / rollback / partial outputs；Config / project setup；Documentation / migration notes
Evidence floor: `uv run pytest -q tests/test_production_scheduler.py -k node22_exact_cycle_wrapper` + selector 选集 + `uv run ruff check .`；node-22 实跑一次 `--plan` 的日志

## Why

2026-11-09 恢复 IFS 2026102300（#3109）时，按 runbook 原样执行 `scripts/ops/node22-run-cycle-once.sh`，失败了两次：

1. 与正在运行的 `nhms-compute-scheduler.service` 争 scheduler 锁，返回 `status=lock_contended`。现在一个 pass 要 1–3 小时，timer 是 `OnUnitActiveSec=5min`，pass 首尾相接，手工执行几乎找不到空档。
2. wrapper 只 source `compute.scheduler-dbfree.env`；systemd drop-in 另外还加载 `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env` 并设置 `NHMS_PYTHON_VENV_BIN`。缺这两项时，`--submit` 的 preflight 返回 `SLURM_GATEWAY_UNAVAILABLE`（没有改动任何状态）。

当时用一次性脚本绕过：停 timer → 等 service 结束 → source secret → submit → 恢复 timer。owner 2026-11-10 决定把这套流程固化到 wrapper 里。

## What Changes

- wrapper 加载与 systemd unit 相同的环境：scheduler env + gateway secret env（默认路径与 tracked drop-in 一致，可覆盖），并导出 `NHMS_PYTHON_VENV_BIN`。
  - `--submit` 时 secret 缺失或不可读 → fail-closed；
  - `--plan` 时缺失 → 只告警。
- wrapper 在调用 python 之前暂停 scheduler timer（只在 timer 原本 active 时），并等 service 离开运行态，等待有上限。
  - 退出时（成功、失败、超时、INT/TERM）恢复本次停掉的 timer。
  - python 改为以子进程运行，不再 `exec`，退出码原样透传。
- systemctl 调用可注入，便于测试。
- runbook 的 exact-cycle 小节，以及其它引用该 wrapper 的 runbook 同步更新。
