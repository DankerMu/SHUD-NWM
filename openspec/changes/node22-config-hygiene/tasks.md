# Tasks: node22-config-hygiene

## 约定（对所有 task 生效）

- **本地验证命令**：`uv run ruff check . && uv run pytest -q <本 task 新增/改动的测试文件>`；改 `docs/**` 或 `infra/**` 文本的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **oracle**：全部为本地 pytest + ruff。node-22 实机动作（安装单元、`systemd-analyze --user verify`、跑 sweep、daemon-reload、测试发信）是运维动作带 receipt，不是任何 task 的验收；服务器升级期间不部署。
- **字节来源**：`infra/systemd/node22/` 的内容逐字节取自只读收据 `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`（含 sha256），不另行 SSH。
- **只读**：本 change 的所有脚本对 systemd 目录只读；对 `infra/env/` 只有 sweep 的 `--apply` 会 `mv -n`，绝不删除。
- **CI 路由**：新增的 `infra/**` 非 Python 文件与新脚本在 `scripts/select_ci_tests.py` 登记 `PathTestRule`，否则 PR CI 退化为 collect-only。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。

## Risk packs（issue-risk-contract.md；selected 的每一项映射到下方某 task 的测试、Verify 命令或显式 non-goal）

- Public API / CLI / script entry — selected：1.2 drift 探针、3.1 handler、4.4 sweep 是新 CLI；退出码矩阵由各自测试覆盖。1.1 不涉及（纯数据 + 守卫测试）。
- Config / project setup — selected：1.1/3.1/3.2 追踪 systemd 单元与 manifest，4.1 gitignore，4.3 env 键分类；全部由守卫测试钉死，部署是运维 non-goal。
- File IO / path safety / overwrite — selected：4.4 `mv -n`、拒绝集归一化、目标已存在退出 2；1.2 只读 systemd 目录（测试以空 `PATH` 证明不调 `systemctl`）。1.1 只新增仓库文件。
- Schema / columns / units / field names — selected：1.1 `MANIFEST.json` 字段 `{deployed_relative_path, tracked_path, sha256}` 是 1.2 探针与 3.x 的契约，由 `tests/test_node22_unit_tracking.py` 断言。 1.2 的 receipt `nhms.node22.unit_drift_check.v1` 键集合由 `tests/test_node22_unit_drift_check.py` 精确断言；探针对 manifest 畸形 fail-closed（退出 2）。
- Auth / permissions / secrets — selected：1.1/3.1 追踪的单元只引用 secret 路径；守卫测试断言无 `*TOKEN|*SECRET|*PASS|*PASSWORD` 赋值且 `EnvironmentFile=` 属 authority 表或 secret 路径。 1.2 的 diff 只打 stdout、receipt 不含文件内容，测试断言 receipt 键集合。
- Concurrency / shared state / ordering — not selected：无共享状态；探针与 sweep 单进程只读/`mv -n`。
- Resource limits / large input / discovery — not selected：输入是 ≤20 个小文件。
- Legacy compatibility / examples — selected：1.1 保留 `infra/systemd/nhms-slurm-gateway.service` 通用模板不改（design D1 rejected alternative）；4.3 effective default 含 legacy fallback 链。
- Error handling / rollback / partial outputs — selected：1.2 与 4.4 的退出码 2 分支、receipt 写失败；2.1 verdict 槽位顺序。1.1 non-goal。
- Release / packaging / dependency compatibility — not selected：无依赖变更。
- Documentation / migration notes — selected：README authority 表/备份策略、runbook 节、DOC_STATUS 标记由 `tests/test_entropy_audit_report_contract.py` 与各 task 的文档断言覆盖；实机部署步骤在 design Migration Plan。 1.2 的 runbook 节只由 `tests/test_entropy_audit_report_contract.py` 文档门覆盖，无内容断言。

## 1. 追踪 node-22 live 单元（spec: node22-unit-tracking）

- [x] 1.1 新建 `infra/systemd/node22/` 放入收据中七个文件的逐字节副本与 `MANIFEST.json`（七个 `node22/` 条目 + 十二个顶层 oneshot `.service`/`.timer` 条目，含 sha256；告警模板条目由 3.1 追加）；新增 `tests/test_node22_unit_tracking.py`；README authority 表下补一条 Notes 指向 `infra/systemd/node22/`；`scripts/select_ci_tests.py` 登记路由。钉死的契约（fixture review 2026-10-17）：

  - **MANIFEST.json 结构**：顶层对象 `{"schema_version": "nhms.node22.unit_manifest.v1", "entries": [...]}`；每条 `{deployed_relative_path, tracked_path, sha256}`；`deployed_relative_path` 相对 `~/.config/systemd/user/`（七个与收据表第一列逐字相同，十二个 oneshot 就是文件名）；`tracked_path` 相对仓库根（七个 `infra/systemd/node22/<deployed_relative_path>`，十二个 `infra/systemd/<name>`）；`sha256` 为仓库内 `tracked_path` 文件内容的小写 hex；`entries` 按 `deployed_relative_path` 字节序排序，3.1"追加"第 20 条即按序插入。1.2 探针与 3.x 都以此结构为契约。
  - **文件字节**：收据每个 ```ini fence 正文 + 恰好一个尾随 LF 即原文件（已核对七个 sha256 一致）；LF 换行、结尾一个换行。十二个 oneshot 文件本身不改。
  - **守卫测试断言**（stdlib only：json/hashlib/re/pathlib；不从 `scripts/` import；仓库根 `Path(__file__).resolve().parents[1]`）：(a) manifest `entries` 包含这 19 条已知 `(deployed_relative_path, tracked_path)`（超集断言，不写 `len == 19`，3.1 加第 20 条时本测试不改）；(b) 每条 sha256 等于 `tracked_path` 文件的 sha256；(c) 七个 `node22/` 条目的 sha256 等于收据表（路径列与 sha256 列去反引号后比对；比对对象是文件内容的 sha256）；(d) 对 `node22/` 下每个文件逐行检查：`Environment=` 值中的每一对 `KEY=VALUE`（含引号、一行多对）若 KEY 以 `TOKEN|SECRET|PASS|PASSWORD` 结尾（大小写敏感、全大写）且 VALUE 非空即违规，`*_FILE` 等不以这几个词结尾的名字与文件名不在范围；只扫 `Environment=`，不扫其它裸 `NAME=value`；(e) 每个非空 `EnvironmentFile=` 值去掉前导 `-` 后，要么等于测试内常量 `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env`，要么等于 `/scratch/frd_muziyao/` + README §"node-22 unit → EnvironmentFile authority (#1694)" 一节（到下一个 `##` 为止）表格第 2 列中反引号包住、以 `NWM/infra/env/` 开头的 token；空 `EnvironmentFile=` 放行；不全文扫 README（否则已退役的 `compute.env` 会被误放行）；(f) 规则函数另以合成行级用例检验：`Environment=SLURM_GATEWAY_TOKEN=x` 拒绝、`Environment="FOO_PASSWORD=y"` 拒绝、`EnvironmentFile=` 放行、`EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/compute.host.env` 放行、`EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.env` 拒绝、文件名 `10-slurm-gateway-token.conf` 不算违规。
  - **README**：不加表格行（第 3 列"Tracked template"指 env 模板）；在该节表下 Notes 增加一条 bullet："部署的单元体与 drop-in 的逐字节副本在 `infra/systemd/node22/`（`MANIFEST.json`，收据 `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`），本表仍是 EnvironmentFile 权威，`tests/test_node22_unit_tracking.py` 以本表校验那些文件的 `EnvironmentFile=`"；三列表结构与 `nhms-required-keys` 块不动（`tests/test_env_templates.py` 解析该块）；措辞不含 Production Topology Hard Gate 扫描的敏感字样（旧 DB 端口、已退役 writer 角色等）。
  - **selector**：新增一行 `PathTestRule("infra/systemd/node22/*", ("tests/test_node22_unit_tracking.py",))`（fnmatch 的 `*` 跨 `/`，先例 `infra/systemd/system/*`）；另外本测试是十二个 oneshot 文件、`infra/env/README.md` 与收据的字面读者：十一个 oneshot 各有一条精确既有行，加上精确的 `infra/env/README.md` 行，共 12 条既有行在 tests tuple 末尾追加本测试（不新开重复 pattern 的行）；`infra/systemd/nhms-scheduler-evidence-retention.timer` 与收据路径没有任何匹配行，各新开一行。合计新增 3 行（`infra/systemd/node22/*`、evidence-retention timer、收据）+ 12 处追加。相应更新 `tests/test_select_ci_tests.py` 中既有 producer 的 exact-set 守卫（允许的测试改动），并为 3 条新行各写一个仿现有 `*_rule_red_when_removed` 的「删掉即红」断言。不改 `infra/systemd/nhms-slurm-gateway.service` 通用模板及其 selector 常量。
  - **PR Boundary 偏离**：issue #2910 写的是"selector 一条规则"；按上面 selector 项扩为新增 3 行 + 12 处追加，原因是 3.2 改六个 `.service` 时 PR CI 必须选中本测试，否则 manifest sha256 错也绿。PR 的偏离记录写明。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_unit_tracking.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py tests/test_env_templates.py`；`find infra/systemd/node22 -type f ! -name MANIFEST.json -print0 | xargs -0 sha256sum` 七行与收据表逐项一致；README 的其它读者（`tests/test_slurm_gateway_deployment_contract.py`、`tests/test_two_node_docker_runtime.py`）由既有 `infra/env/README.md` 路由在 PR CI 上选中。

  **Suggested fixture level:** compact - 纯数据文件加一个守卫测试，无运行时代码。

  **Minimal mergeable slice:** atomic - manifest、七个文件与守卫测试互为 oracle，缺一则守卫要么空跑要么立刻红。

- [x] 1.2 新增 `scripts/node22_unit_drift_check.py`（manifest 驱动；`--manifest`、`--systemd-user-dir`、`--receipt`；按 design D2 的扫描范围、分类与退出码矩阵；`differs` 打印 unified diff）与 `tests/test_node22_unit_drift_check.py` 覆盖 spec 六个 scenario；`node22-control-plane-manual-recovery.md` 增加「改单元前后各跑一次 drift 探针」步骤；selector 登记；并吸收 PR #2966 审查推迟到 #2911 的三项守卫加固。钉死的契约（fixture review 2026-10-17）：

  - **运行与消费者**：运维在 node-22 手动运行（Migration Plan 首跑、改单元前后各一次），无 timer、无自动读者；receipt 附到 `docs/runbooks/receipts/` 的运维收据。脚本 stdlib only、不 import `nhms`/`subprocess`、兼容 3.11/3.12（node-22 是 3.12.7 `.venv`）。
  - **路径解析**：仓库根 = `Path(manifest).resolve().parents[3]`（manifest 在 `infra/systemd/node22/MANIFEST.json`）；`--manifest` 默认 `Path(__file__).resolve().parents[1] / "infra/systemd/node22/MANIFEST.json"`；`--systemd-user-dir` 默认 `$XDG_CONFIG_HOME/systemd/user`（仅当变量非空且为绝对路径），否则 `~/.config/systemd/user`。
  - **manifest 校验（探针侧，fail-closed → 退出 2）**：`schema_version == nhms.node22.unit_manifest.v1`；每条恰好三个键；两个路径字段非绝对、无 `..` 段；`tracked_path` 以 `infra/systemd/` 开头；`deployed_relative_path` 无重复且最多一层目录；tracked 文件 sha256 必须等于条目（tracked 侧不可信即退出 2 并点名条目）。一个用例把真实 `MANIFEST.json` 过一遍校验器。
  - **分类**：`missing` 仅指 deployed 路径 `FileNotFoundError`；`PermissionError`/其它 `OSError`/deployed 路径是目录或非常规文件 → 退出 2；manifest 条目的 deployed 路径若是符号链接则透过链接按内容分类。untracked 扫描：顶层 `nhms-*.service`、`nhms-*.timer` 与一层 `nhms-*.service.d/*.conf`（精确后缀 + `nhms-` 前缀；无 `.timer.d`；`*.service.pre-*`、`*.conf.<date>` 永不匹配）；`is_symlink()` 为真的一律跳过（含同名顶层链接、链接目录、`*.wants/`）。退出 2 优先于 1。
  - **diff**：只打 stdout，`difflib.unified_diff`，标签 `a/<tracked_path>`、`b/<deployed_relative_path>`，显示用 `utf-8` + `errors="replace"` 解码，分类仍按字节。receipt 不含任何文件内容（secret 贴进 drop-in 也不会进 receipt）。
  - **receipt**：`--receipt` 必填文件路径，无环境变量默认；父目录不存在或不可检视、所在位置（resolve 父目录 + 原文件名）或完全 resolve 后落在 systemd 目录之下、已存在但不是普通文件（symlink/目录/FIFO，`lstat` 不跟随）→ 在任何读写前退出 2；systemd 目录不可检视也退出 2；临时文件 + `os.replace`，mode 0600；字段 `schema_version`（`nhms.node22.unit_drift_check.v1`）、`generated_at`（UTC ISO-8601 `Z` 后缀，沿用 stall 探针）、`status`（`ok|drift|probe_failed`）、`exit_code`、`manifest_path`、`manifest_sha256`（原始字节 hex）、`systemd_user_dir`、`entries`（`[{deployed_relative_path, tracked_path, status}]` 按 manifest 顺序）、`untracked`（排序后的相对路径）、`errors`；`sort_keys`、结尾一个换行。退出 2 时只要路径可写就照样写 receipt（`status: probe_failed` + `errors` + 已分类条目）；只有写 receipt 失败时不产 receipt，`status: probe_failed` 打到 stderr。
  - **测试证据**：六个 scenario 各一用例；「Backup directories are ignored」按 spec 重建 entropy-audit 记录的真实残留（含 `*.wants/` 符号链接）并断言退出 0、`untracked` 空；exit-2 参数化：manifest 缺失 / `entries: []` / 非法 JSON / 每种 manifest 畸形 / systemd 目录不存在 / tracked 文件缺失 / deployed 文件 chmod 000（root 下 skip）/ receipt 父目录不存在 / receipt 在 systemd 目录下 / drift + 不可读文件同时出现；receipt 键集合精确断言；只读证明：drift 运行前后对 systemd 树做（路径、字节、mtime）快照并断言相等；no-systemctl 证明：`env={"PATH": ""}` 用 `sys.executable` 运行脚本并显式传三个参数（`HOME` 未设），另静态断言脚本不 import `subprocess`。
  - **吸收 #2966 推迟项（允许改 `tests/test_node22_unit_tracking.py`）**：(1) `directive_value` 在第一个 `=` 处 partition、key/value 两侧 strip、扫描前拼接 `\` 续行；三条合成用例 HEAD 红修后绿：`Environment = X_TOKEN=abc` 拒绝、`EnvironmentFile = /scratch/frd_muziyao/NWM/infra/env/compute.env` 拒绝、`Environment=A=1 \` + 下一行 `X_TOKEN=abc` 拒绝；(2) shape 测试加断言：`tracked_path` 以 `infra/systemd/` 开头，两个路径字段非绝对且无 `..`；(3) `tracked_node22_files()` 跳过 `.` 开头文件，加 tmp 目录含 `.DS_Store` 的用例。
  - **runbook**：在 `## 已知限制` 之前新增一小节；命令用 `/scratch/frd_muziyao/NWM/.venv/bin/python scripts/node22_unit_drift_check.py --receipt <systemd 目录之外的路径>`（node-22 禁止 `uv run`）；写明 0/1/2 含义；该节只由 `tests/test_entropy_audit_report_contract.py` 的文档门覆盖。
  - **selector**：pin 行 `PathTestRule("scripts/node22_unit_drift_check.py", ("tests/test_node22_unit_drift_check.py",))`（仿 stall 探针 pin）；`infra/systemd/node22/*` 行追加 drift 测试（它读真实 `MANIFEST.json`）；runbook 行不追加（无测试读它）；`tests/test_select_ci_tests.py` 更新 exact-set 守卫并为 pin 行加「删掉即红」断言。
  - **PR Boundary 偏离**：issue 写「一个测试、selector 一行」；本 task 还改 `tests/test_node22_unit_tracking.py`（推迟项）与 2 处 selector。PR 的偏离记录写明。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_drift_check.py tests/test_node22_unit_tracking.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py`；测试以空 `PATH` 运行脚本证明不调用 `systemctl`。

  **Suggested fixture level:** compact - 新增独立 CLI，爆炸半径是它自己的退出码。

  **Minimal mergeable slice:** atomic - 退出码矩阵、receipt 与分类是同一个判定函数的分支，拆开没有可独立验证的中间态。

## 2. stall 探针的 root-blocked 谓词（spec: scheduler-stall-alerting）

- [ ] 2.1 `scripts/node22_scheduler_stall_health.py`：`PassRecord` 增加 `execution_boundary`；新增 `root_blocked_run()`、env `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`（默认 60，`load_config` 校验下限 10 与可达上限）、verdict `scheduler_root_blocked` 插在 `lock_contended_persistent` 之后、`submission_stalled` 之前，runbook 锚 `stall-scheduler-root-blocked`，receipt 增加 `root_blocked_run` 块；pass 形态与 `shapes` 字典不变。`tests/test_node22_scheduler_stall_health.py` 新增 spec 的全部 scenario（fixture 必须不带 `progress_guard`，13 个 pass 显式时间戳间隔 5 分钟）；既有用例期望值不变；探针 docstring 中"preflight-blocked pass is fully observed"改为区分 pre-lock 与 post-candidate。`docs/runbooks/production-ops/stuck-detection.md` 新增 `stall-scheduler-root-blocked` 节（处置：逐个检查 `NHMS_SCHEDULER_ALLOWED_ROOTS` 与 registry manifest 根的存在与挂载，指向 `node22-control-plane-manual-recovery.md`）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_stall_health.py tests/test_entropy_audit_report_contract.py`；新增一个用例直接复用 2026-10-13 之后某个真实 pass artifact 的字段子集（`execution_boundary: scheduler_root_preflight_blocked`、counts 全 0、无 `progress_guard`），断言退出码 1。

  **Suggested fixture level:** expanded - 告警 lane 的判定谓词与优先级改动；observer/observed 谓词一致性风险轴直接命中。

  **Minimal mergeable slice:** atomic - 谓词、阈值校验、verdict 槽位与 receipt 块是一条判定链；只加谓词不加 verdict 没有可观测结果。

## 3. node-22 单元失败告警（spec: node22-unit-failure-alert）

- [ ] 3.1 新增 `infra/systemd/nhms-node22-unit-failure-alert@.service`（绝对路径）、`scripts/node22_unit_failure_alert_once.sh`、`infra/env/node22-unit-failure-alert.example`（恰好七个键，`NHMS_FRONTIER_SENDMAIL=/usr/sbin/sendmail`）；README authority 表补一行；`MANIFEST.json` 追加模板条目（第 20 条）；selector 登记。新增 `tests/test_node22_unit_failure_alert.py`：假 transport 断言恰好一封含单元名与 journal 尾行；无收件人 `SKIPPED` 退出 0 且不调用 transport；transport 退出 1 时 handler 非 0；example 键集合精确；节点 22 单元与模板不含 `%h/NWM`；manifest 含模板 sha256。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_node22_unit_tracking.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py`；`bash -n scripts/node22_unit_failure_alert_once.sh`。

  **Suggested fixture level:** compact - 复制已在 27 运行的模式到 22，改动面是路径、键集合与失败退出。

  **Minimal mergeable slice:** atomic - 模板单元、handler 与 example 三者缺一则无法被任何单元消费。

- [ ] 3.2 为 `infra/systemd/` 下六个 node-22 oneshot 单元各加一行 `OnFailure=nhms-node22-unit-failure-alert@%n.service`，同 PR 更新 `MANIFEST.json` 中这六个文件的 sha256；`tests/test_node22_unit_failure_alert.py` 增加 wiring 断言（恰好这六个各一行；gateway 单元与 `infra/systemd/node22/**` 为零；manifest sha256 一致）。

  Depends on: 3.1, 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_node22_unit_tracking.py tests/test_entropy_audit_report_contract.py`。

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
