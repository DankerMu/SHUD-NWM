# Tasks: node22-config-hygiene

## 约定（对所有 task 生效）

- **本地验证命令**：`uv run ruff check . && uv run pytest -q <本 task 新增/改动的测试文件>`；改 `docs/**` 或 `infra/**` 文本的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **oracle**：全部为本地 pytest + ruff。node-22 实机动作（安装单元、`systemd-analyze --user verify`、跑 sweep、daemon-reload、测试发信）是运维动作带 receipt，不是任何 task 的验收；服务器升级期间不部署。
- **字节来源**：`infra/systemd/node22/` 的内容逐字节取自只读收据 `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`（含 sha256），不另行 SSH。
- **只读**：本 change 的所有脚本对 systemd 目录只读；对 `infra/env/` 只有 sweep 的 `--apply` 会 `mv -n`，绝不删除。
- **CI 路由**：新增的 `infra/**` 非 Python 文件与新脚本在 `scripts/select_ci_tests.py` 登记 `PathTestRule`，否则 PR CI 退化为 collect-only。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。

## 1. 追踪 node-22 live 单元（spec: node22-unit-tracking）

- [ ] 1.1 新建 `infra/systemd/node22/` 放入收据中七个文件的逐字节副本与 `MANIFEST.json`（七个 `node22/` 条目 + 十二个顶层 oneshot `.service`/`.timer` 条目，含 sha256；告警模板条目由 3.1 追加）；新增 `tests/test_node22_unit_tracking.py`（sha256 对照 manifest 与收据表；无 secret 值；`EnvironmentFile=` 归一化后属 README authority 表或 secret 路径，空重置行放行）；README authority 表补一行指向 `infra/systemd/node22/`；`scripts/select_ci_tests.py` 为 `infra/systemd/node22/**` 登记规则。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_unit_tracking.py tests/test_select_ci_tests.py`；`sha256sum infra/systemd/node22/*` 与收据表逐项一致。

  **Suggested fixture level:** compact - 纯数据文件加一个守卫测试，无运行时代码。

  **Minimal mergeable slice:** atomic - manifest、七个文件与守卫测试互为 oracle，缺一则守卫要么空跑要么立刻红。

- [ ] 1.2 新增 `scripts/node22_unit_drift_check.py`（manifest 驱动；`--manifest`、`--systemd-user-dir`、`--receipt`；按 design D2 的扫描范围、分类与退出码矩阵；`differs` 打印 unified diff）与 `tests/test_node22_unit_drift_check.py` 覆盖 spec 六个 scenario；`node22-control-plane-manual-recovery.md` 增加"改单元前后各跑一次 drift 探针"步骤；selector 登记。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_drift_check.py tests/test_select_ci_tests.py`；测试以空 `PATH` 运行脚本证明不调用 `systemctl`。

  **Suggested fixture level:** compact - 新增独立 CLI，爆炸半径是它自己的退出码。

  **Minimal mergeable slice:** atomic - 退出码矩阵、receipt 与分类是同一个判定函数的分支，拆开没有可独立验证的中间态。

## 2. stall 探针的 root-blocked 谓词（spec: scheduler-stall-alerting）

- [ ] 2.1 `scripts/node22_scheduler_stall_health.py`：`PassRecord` 增加 `execution_boundary`；新增 `root_blocked_run()`、env `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`（默认 60，`load_config` 校验下限 10 与可达上限）、verdict `scheduler_root_blocked` 插在 `lock_contended_persistent` 之后、`submission_stalled` 之前，runbook 锚 `stall-scheduler-root-blocked`，receipt 增加 `root_blocked_run` 块；pass 形态与 `shapes` 字典不变。`tests/test_node22_scheduler_stall_health.py` 新增 spec 的全部 scenario（fixture 必须不带 `progress_guard`，13 个 pass 显式时间戳间隔 5 分钟）；既有用例期望值不变；探针 docstring 中"preflight-blocked pass is fully observed"改为区分 pre-lock 与 post-candidate。`docs/runbooks/production-ops/stuck-detection.md` 新增 `stall-scheduler-root-blocked` 节（处置：逐个检查 `NHMS_SCHEDULER_ALLOWED_ROOTS` 与 registry manifest 根的存在与挂载，指向 `node22-control-plane-manual-recovery.md`）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_stall_health.py`；新增一个用例直接复用 2026-10-17 真实 artifact 的字段子集（`execution_boundary: scheduler_root_preflight_blocked`、counts 全 0、无 `progress_guard`），断言退出码 1。

  **Suggested fixture level:** expanded - 告警 lane 的判定谓词与优先级改动；observer/observed 谓词一致性风险轴直接命中。

  **Minimal mergeable slice:** atomic - 谓词、阈值校验、verdict 槽位与 receipt 块是一条判定链；只加谓词不加 verdict 没有可观测结果。

## 3. node-22 单元失败告警（spec: node22-unit-failure-alert）

- [ ] 3.1 新增 `infra/systemd/nhms-node22-unit-failure-alert@.service`（绝对路径）、`scripts/node22_unit_failure_alert_once.sh`、`infra/env/node22-unit-failure-alert.example`（恰好七个键，`NHMS_FRONTIER_SENDMAIL=/usr/sbin/sendmail`）；README authority 表补一行；`MANIFEST.json` 追加模板条目（第 20 条）；selector 登记。新增 `tests/test_node22_unit_failure_alert.py`：假 transport 断言恰好一封含单元名与 journal 尾行；无收件人 `SKIPPED` 退出 0 且不调用 transport；transport 退出 1 时 handler 非 0；example 键集合精确；节点 22 单元与模板不含 `%h/NWM`；manifest 含模板 sha256。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_node22_unit_tracking.py tests/test_select_ci_tests.py`；`bash -n scripts/node22_unit_failure_alert_once.sh`。

  **Suggested fixture level:** compact - 复制已在 27 运行的模式到 22，改动面是路径、键集合与失败退出。

  **Minimal mergeable slice:** atomic - 模板单元、handler 与 example 三者缺一则无法被任何单元消费。

- [ ] 3.2 为 `infra/systemd/` 下六个 node-22 oneshot 单元各加一行 `OnFailure=nhms-node22-unit-failure-alert@%n.service`，同 PR 更新 `MANIFEST.json` 中这六个文件的 sha256；`tests/test_node22_unit_failure_alert.py` 增加 wiring 断言（恰好这六个各一行；gateway 单元与 `infra/systemd/node22/**` 为零；manifest sha256 一致）。

  Depends on: 3.1, 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_node22_unit_tracking.py`。

  **Suggested fixture level:** none - 六行声明式配置加断言与 manifest 更新。

  **Minimal mergeable slice:** atomic - 六行同一语义，wiring 断言一次钉死集合；manifest 必须同 PR 否则 1.1 的 sha256 测试红。

## 4. env 卫生（spec: node22-env-hygiene）

- [ ] 4.1 `.gitignore` 增加 `.nhms-work/`；新增 `tests/test_gitignore_nhms_work.py` 用 `git check-ignore -q` 断言。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_gitignore_nhms_work.py`。

  **Suggested fixture level:** none - 一行 ignore 规则。

  **Minimal mergeable slice:** atomic - 单行。

- [ ] 4.2 `docs/runbooks/two-node-deployment-overview.md` §5.2：列表上方加"`infra/env/compute.env` 已退役、无单元加载、authority 见 `infra/env/README.md`"一句与完整六字段 section 标记，列表原样保留在标记区间内；新增 `tests/test_node22_runbook_section_markers.py` 用 `_section_archive_status_marker_ranges` 断言恰好一个完整 `section` 区间覆盖列表。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_runbook_section_markers.py tests/test_entropy_audit_report_contract.py`。

  **Suggested fixture level:** compact - 文档标记加一个解析断言。

  **Minimal mergeable slice:** atomic - 标记与断言互为 oracle。

- [ ] 4.3 `infra/env/README.md` 在 required-key 块外新增 live-drift 分类表（七个键；status 三选一；"effective value when unset"按 loader 实际取值，含 legacy fallback，如 `LEASE_GUARD_MODE` → `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` → `flock`）；`required` 行在 `node22-control-plane-manual-recovery.md` 补操作步骤。新增 `tests/test_node22_scheduler_env_drift_table.py`：表内键均在 example；每个 effective default 通过调用 loader（去掉该键）核实。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_env_drift_table.py tests/test_env_templates.py tests/test_entropy_audit_report_contract.py`。

  **Suggested fixture level:** compact - 文档契约加一个对照 loader 的测试。

  **Minimal mergeable slice:** atomic - 七行分类是一张表，表与测试同生。

- [ ] 4.4 `infra/env/README.md` 新增"Backup copies"节；新增 `scripts/ops/node22_env_backup_sweep.sh`（`--env-dir`、`--systemd-user-dir`、`--backup-root`、`--home`、`--apply`；候选模式、归一化拒绝集、plan 行、receipt、`mv -n`、目标已存在退出 2，按 design D5）与 `tests/test_node22_env_backup_sweep.py` 覆盖 spec 四个 scenario；`node22-control-plane-manual-recovery.md` 补"先 dry-run 后 --apply、附 receipt"步骤；selector 登记。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_env_backup_sweep.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py`；`bash -n scripts/ops/node22_env_backup_sweep.sh`。

  **Suggested fixture level:** compact - 新增独立脚本，File IO 风险包（mv -n、拒绝集归一化）由测试覆盖。

  **Minimal mergeable slice:** atomic - dry-run、拒绝、apply 是同一候选集的三个出口，缺任一则脚本对运维不可用；策略一节是脚本目标目录的定义。
