# Tasks: node22-config-hygiene

## 约定（对所有 task 生效）

- **本地验证命令**：`uv run ruff check . && uv run pytest -q <本 task 新增/改动的测试文件>`；改 `docs/**` 或 `infra/**` 文本的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **oracle**：全部为本地 pytest + ruff。node-22 实机动作（安装单元、跑 sweep、daemon-reload）是运维动作带 receipt，不是任何 task 的验收；服务器升级期间不部署。
- **只读**：本 change 的所有脚本对 systemd 目录只读；对 `infra/env/` 只有 sweep 的 `--apply` 会 `mv`，绝不删除。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。

## 1. 追踪 node-22 live 单元（spec: node22-unit-tracking）

- [ ] 1.1 新建 `infra/systemd/node22/`，放入 `nhms-compute-api.service`、`nhms-compute-scheduler.service`、`nhms-compute-scheduler.timer`、`nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`、`nhms-compute-scheduler.service.d/20-runtime-path.conf`、`nhms-slurm-gateway.service.d/10-node22-live.conf` 的逐字节副本（来源：2026-10-16 只读 SSH 读取的部署文件，内容已在审计报告 §4 记录；secret 仅保留路径）。新增 `tests/test_node22_unit_tracking.py`：断言无 `TOKEN/SECRET/PASS/PASSWORD` 非空赋值，断言每个 `EnvironmentFile=` 路径在 `infra/env/README.md` 的 node-22 authority 表或 documented secret 路径中。README 的 authority 表补一行指向 `infra/systemd/node22/`。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_unit_tracking.py`；`grep -rn "TOKEN=" infra/systemd/node22/` 只命中空值或变量名引用。

  **Suggested fixture level:** compact - 纯数据文件加一个守卫测试，无运行时代码。

  **Minimal mergeable slice:** atomic - 六个文件互为 drop-in 关系，缺一则 `systemd-analyze verify` 对不上部署态；守卫测试与数据同 PR 才有意义。

- [ ] 1.2 新增 `scripts/node22_unit_drift_check.py`（read-only；`--tracked-dir` 默认 `infra/systemd/node22`，`--systemd-user-dir` 默认 `$XDG_CONFIG_HOME/systemd/user` 或 `~/.config/systemd/user`，`--receipt` 路径），按 design D2 输出 `same/differs/missing/untracked` 与 receipt `nhms.node22.unit_drift_check.v1`，退出码 0/1/2；`differs` 打印 unified diff。新增 `tests/test_node22_unit_drift_check.py` 覆盖 spec 四个 scenario。`docs/runbooks/node22-control-plane-manual-recovery.md` 增加"改单元前后各跑一次 drift 探针"步骤。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_drift_check.py`；脚本对 tmp 目录运行不调用 `systemctl`（测试用 `PATH` 置空断言）。

  **Suggested fixture level:** compact - 新增独立 CLI，无共享入口改动，爆炸半径是它自己的退出码。

  **Minimal mergeable slice:** atomic - 退出码契约、receipt 与四种分类是同一个判定函数的四个分支，拆开没有可独立验证的中间态。

## 2. stall 探针的 root-blocked 形态（spec: scheduler-stall-alerting）

- [ ] 2.1 在 `scripts/node22_scheduler_stall_health.py` 的 pass 分类里读取 `execution_boundary`，新增 `SHAPE_ROOT_BLOCKED`；新增时间阈值常量与 env `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`（默认 60）、verdict `scheduler_root_blocked`、runbook section `stall-scheduler-root-blocked`，按 design D3 的优先级插入；`root-blocked` pass 对 no-submission streak 既不重置也不计数（与 `resource_limit_blocked` 同一处理路径）。`tests/test_node22_scheduler_stall_health.py` 新增 spec 四个 scenario；`docs/runbooks/production-ops/stuck-detection.md` 新增 `stall-scheduler-root-blocked` 节（处置：检查 `NHMS_SCHEDULER_ALLOWED_ROOTS` 各 root 与挂载状态，指向 `node22-control-plane-manual-recovery.md`）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_stall_health.py`；用审计报告 §0 的真实 artifact 形态（`execution_boundary: scheduler_root_preflight_blocked`，counts 全 0，13 个 pass 跨 65 分钟）作为 fixture，断言退出码 1；既有全部用例期望值不变。

  **Suggested fixture level:** expanded - 告警 lane 的判定谓词与优先级改动；project-profile 的 observer/observed 谓词一致性风险轴直接命中。

  **Minimal mergeable slice:** atomic - 形态、阈值、verdict 与优先级是一条判定链；只加形态不加 verdict 会让新形态落入 idle，行为反而更错。

## 3. node-22 单元失败告警（spec: node22-unit-failure-alert）

- [ ] 3.1 新增 `infra/systemd/nhms-node22-unit-failure-alert@.service`、`scripts/node22_unit_failure_alert_once.sh`、`infra/env/node22-unit-failure-alert.example`，按 design D4 镜像 node-27 对应三件（WorkingDirectory、EnvironmentFile 路径改为 node-22；变量名与 node-27 frontier-alert lane 一致；未配置收件人退出 0）。新增 `tests/test_node22_unit_failure_alert.py`：用假 sendmail shim 断言恰好一封、含单元名与 journal 尾行；无收件人时退出 0 且不调用 shim。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py`；`bash -n scripts/node22_unit_failure_alert_once.sh`。

  **Suggested fixture level:** compact - 复制已在 27 运行的模式到 22，改动面是路径与 env 文件名。

  **Minimal mergeable slice:** atomic - 模板单元、handler 与 example 三者缺一则无法被任何单元消费。

- [ ] 3.2 为 `infra/systemd/` 下六个 node-22 oneshot 单元各加一行 `OnFailure=nhms-node22-unit-failure-alert@%n.service`；`tests/test_node22_unit_failure_alert.py` 增加 wiring 断言（恰好这六个有且只有一行；gateway 单元与 `infra/systemd/node22/**` 没有）。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py`。

  **Suggested fixture level:** none - 六行声明式配置加一个断言。

  **Minimal mergeable slice:** atomic - 六行同一语义，拆开只会制造六个一行 PR；wiring 断言一次钉死集合。

## 4. env 卫生（spec: node22-env-hygiene）

- [ ] 4.1 `.gitignore` 增加 `.nhms-work/`；新增 `tests/test_gitignore_nhms_work.py`（或并入既有 gitignore 测试）用 `git check-ignore` 断言。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_gitignore_nhms_work.py`。

  **Suggested fixture level:** none - 一行 ignore 规则。

  **Minimal mergeable slice:** atomic - 单行。

- [ ] 4.2 `docs/runbooks/two-node-deployment-overview.md` §5.2 加 DOC_STATUS section 标记并补"`infra/env/compute.env` 已退役、无单元加载、authority 见 `infra/env/README.md`"一句；`infra/README.two-node-docker.md` 顶部提示补一行指向同一 authority。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py`；`uv run python scripts/governance/audit_repo_entropy.py --format json` 对该文件无新增 finding。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 一个标记块加一句话。

- [ ] 4.3 `infra/env/README.md`：新增"Backup copies"节（备份只放 `~/NWM-presync-backup-<UTC date>/infra-env/`）；required-key 块对七个 live 缺失键逐一标 `required` 或 `optional (code default: X)`（默认值从消费代码读取）；`required` 键在 `node22-control-plane-manual-recovery.md` 补操作步骤。新增 `tests/test_node22_scheduler_env_example_contract.py`：块内键均出现在 example，`optional` 默认值与代码常量一致。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_env_example_contract.py tests/test_entropy_audit_report_contract.py`。

  **Suggested fixture level:** compact - 文档契约加一个对照代码常量的测试。

  **Minimal mergeable slice:** atomic - 七个键的分类是一张表，表与测试同生。

- [ ] 4.4 新增 `scripts/ops/node22_env_backup_sweep.sh`（dry-run 默认，`--apply`，`--env-dir`、`--systemd-user-dir`、`--backup-root` 可覆盖，候选模式 `*.bak*`/`*.backup-*`/`*.pre-*`，被任一 `EnvironmentFile=` 引用则退出 2 并点名单元，receipt JSON，`mv` 不删除）；新增 `tests/test_node22_env_backup_sweep.py` 覆盖 spec 三个 scenario；`node22-control-plane-manual-recovery.md` 补"先 dry-run 后 --apply、附 receipt"步骤。

  Depends on: 4.3

  Verify：`uv run pytest -q tests/test_node22_env_backup_sweep.py`；`bash -n scripts/ops/node22_env_backup_sweep.sh`。

  **Suggested fixture level:** compact - 新增独立脚本，File IO 风险包（mv、拒绝清单）由测试覆盖。

  **Minimal mergeable slice:** atomic - dry-run、拒绝、apply 是同一候选集的三个出口，缺任一则脚本对运维不可用。
