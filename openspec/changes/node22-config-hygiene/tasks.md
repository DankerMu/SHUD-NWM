# Tasks: node22-config-hygiene

## 约定（对所有 task 生效）

- **本地验证命令**：`uv run ruff check . && uv run pytest -q <本 task 新增/改动的测试文件>`；改 `docs/**` 或 `infra/**` 文本的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **oracle**：全部为本地 pytest + ruff。node-22 实机动作（安装单元、`systemd-analyze --user verify`、跑 sweep、daemon-reload、测试发信）是运维动作带 receipt，不是任何 task 的验收；服务器升级期间不部署。
- **字节来源**：`infra/systemd/node22/` 的内容逐字节取自只读收据 `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`（含 sha256），不另行 SSH。
- **只读**：本 change 的所有脚本对 systemd 目录只读；对 `infra/env/` 只有 sweep 的 `--apply` 会 `mv -n`，绝不删除。
- **CI 路由**：新增的 `infra/**` 非 Python 文件与新脚本在 `scripts/select_ci_tests.py` 登记 `PathTestRule`，否则 PR CI 退化为 collect-only。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。

## Risk packs（issue-risk-contract.md；selected 的每一项映射到下方某 task 的测试、Verify 命令或显式 non-goal）

- Public API / CLI / script entry — selected：1.2 drift 探针、3.1 handler、4.4 sweep 是新 CLI；退出码矩阵由各自测试覆盖。1.1 不涉及（纯数据 + 守卫测试）。2.1 的新 verdict 改变探针退出状态与 receipt，由 scenario 测试覆盖。
- Config / project setup — selected：1.1/3.1/3.2 追踪 systemd 单元与 manifest，4.1 gitignore，4.3 env 键分类；全部由守卫测试钉死，部署是运维 non-goal。2.1 新 env 键默认值在脚本内，stall 单元无 `EnvironmentFile=`，部署无变化；校验用例覆盖范围。
- File IO / path safety / overwrite — selected：4.4 `mv -n`、拒绝集归一化、目标已存在退出 2；1.2 只读 systemd 目录（测试以空 `PATH` 证明不调 `systemctl`）。1.1 只新增仓库文件。
- Schema / columns / units / field names — selected：1.1 `MANIFEST.json` 字段 `{deployed_relative_path, tracked_path, sha256}` 是 1.2 探针与 3.x 的契约，由 `tests/test_node22_unit_tracking.py` 断言。 1.2 的 receipt `nhms.node22.unit_drift_check.v1` 键集合由 `tests/test_node22_unit_drift_check.py` 精确断言；探针对 manifest 畸形 fail-closed（退出 2）。 2.1 的 `PassRecord.__slots__` 新字段与顶层 receipt 块 `root_blocked_run`（四键精确断言）为增量，`RECEIPT_SCHEMA_VERSION` 保持 v1。
- Auth / permissions / secrets — selected：1.1/3.1 追踪的单元只引用 secret 路径；守卫测试断言无 `*TOKEN|*SECRET|*PASS|*PASSWORD` 赋值且 `EnvironmentFile=` 属 authority 表或 secret 路径。 1.2 的 diff 只打 stdout、receipt 不含文件内容，测试断言 receipt 键集合。 4.4 搬走的备份可能含明文凭据：`umask 077`、目标目录 0700（测试断言）。
- Concurrency / shared state / ordering — not selected：无共享状态；探针与 sweep 单进程只读/`mv -n`。
- Resource limits / large input / discovery — not selected：输入是 ≤20 个小文件。
- Legacy compatibility / examples — selected：1.1 保留 `infra/systemd/nhms-slurm-gateway.service` 通用模板不改（design D1 rejected alternative）；4.3 effective default 含 legacy fallback 链。2.1 无 `execution_boundary` 或非字符串的旧 artifact 读为 `None`、绝不 `probe_failed`（既有 `_pass_payload` fixture 全部无该字段，既有期望值不变即证据）。
- Error handling / rollback / partial outputs — selected：1.2 与 4.4 的退出码 2 分支、receipt 写失败；2.1 verdict 槽位顺序。1.1 non-goal。
- Release / packaging / dependency compatibility — not selected：无依赖变更。
- Documentation / migration notes — selected：README authority 表/备份策略、runbook 节、DOC_STATUS 标记由 `tests/test_entropy_audit_report_contract.py` 与各 task 的文档断言覆盖；实机部署步骤在 design Migration Plan。 3.1 的 README authority 行由 `tests/test_node22_unit_failure_alert.py` 断言；3.2 把 stall 探针 docstring 与 stuck-detection runbook 中 "node-22 无 `OnFailure=` 通道" 的陈述改为已接线（钉死块列出两处）。 1.2 的 runbook 节只由 `tests/test_entropy_audit_report_contract.py` 文档门覆盖，无内容断言。 2.1 的 runbook 锚由 `test_r2` 遍历 `RUNBOOK_ANCHORS` 校验。

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
  - **测试证据**：六个 scenario 各一用例；「Backup directories are ignored」按 spec 重建 entropy-audit 记录的真实残留（含 `*.wants/` 符号链接）并断言退出 0、`untracked` 空；exit-2 参数化：manifest 缺失 / `entries: []` / 非法 JSON / 每种 manifest 畸形 / systemd 目录不存在 / tracked 文件缺失 / deployed 文件 chmod 000（root 下 skip）/ receipt 父目录不存在或不可检视 / receipt 所在位置或解析后在 systemd 目录下 / receipt 已存在但为 symlink、目录或 FIFO / systemd 目录不可检视 / drift + 不可读文件同时出现；receipt 键集合精确断言；只读证明：drift 运行前后对 systemd 树做（路径、字节、mtime）快照并断言相等；no-systemctl 证明：`env={"PATH": ""}` 用 `sys.executable` 运行脚本并显式传三个参数（`HOME` 未设），另静态断言脚本不 import `subprocess`。
  - **吸收 #2966 推迟项（允许改 `tests/test_node22_unit_tracking.py`）**：(1) `directive_value` 在第一个 `=` 处 partition、key/value 两侧 strip、扫描前拼接 `\` 续行；三条合成用例 HEAD 红修后绿：`Environment = X_TOKEN=abc` 拒绝、`EnvironmentFile = /scratch/frd_muziyao/NWM/infra/env/compute.env` 拒绝、`Environment=A=1 \` + 下一行 `X_TOKEN=abc` 拒绝；(2) shape 测试加断言：`tracked_path` 以 `infra/systemd/` 开头，两个路径字段非绝对且无 `..`；(3) `tracked_node22_files()` 跳过 `.` 开头文件，加 tmp 目录含 `.DS_Store` 的用例。
  - **runbook**：在 `## 已知限制` 之前新增一小节；命令用 `/scratch/frd_muziyao/NWM/.venv/bin/python scripts/node22_unit_drift_check.py --receipt <systemd 目录之外的路径>`（node-22 禁止 `uv run`）；写明 0/1/2 含义；该节只由 `tests/test_entropy_audit_report_contract.py` 的文档门覆盖。
  - **selector**：pin 行 `PathTestRule("scripts/node22_unit_drift_check.py", ("tests/test_node22_unit_drift_check.py",))`（仿 stall 探针 pin）；`infra/systemd/node22/*` 行追加 drift 测试（它读真实 `MANIFEST.json`）；runbook 行不追加（无测试读它）；`tests/test_select_ci_tests.py` 更新 exact-set 守卫并为 pin 行加「删掉即红」断言。
  - **PR Boundary 偏离**：issue 写「一个测试、selector 一行」；本 task 还改 `tests/test_node22_unit_tracking.py`（推迟项）与 2 处 selector。PR 的偏离记录写明。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_drift_check.py tests/test_node22_unit_tracking.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py`；测试以空 `PATH` 运行脚本证明不调用 `systemctl`。

  **Suggested fixture level:** compact - 新增独立 CLI，爆炸半径是它自己的退出码。

  **Minimal mergeable slice:** atomic - 退出码矩阵、receipt 与分类是同一个判定函数的分支，拆开没有可独立验证的中间态。

## 2. stall 探针的 root-blocked 谓词（spec: scheduler-stall-alerting）

- [x] 2.1 `scripts/node22_scheduler_stall_health.py`：`PassRecord` 增加 `execution_boundary`；新增 `root_blocked_run()`、env `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`（默认 60，`load_config` 校验下限 10 与可达上限）、verdict `scheduler_root_blocked` 插在 `lock_contended_persistent` 之后、`submission_stalled` 之前，runbook 锚 `stall-scheduler-root-blocked`，receipt 增加 `root_blocked_run` 块；pass 形态与 `shapes` 字典不变。`tests/test_node22_scheduler_stall_health.py` 新增 spec 的全部 scenario；既有用例期望值不变；探针 docstring 中「preflight-blocked pass is fully observed」改为区分 pre-lock 与 post-candidate。`docs/runbooks/production-ops/stuck-detection.md` 新增 `stall-scheduler-root-blocked` 节。钉死的契约（fixture review 2026-10-17）：

  - **谓词**：run 从 `records[0]`（最新）起算，只走 `records[:config.streak_window]`；newest 不是 root-blocked 则 `passes=0`、`span_minutes=None`、`newest_is_root_blocked=False`（恢复后的 lane 结构上不会告警）；`span_minutes` 为 float，按 `started_at` 从最旧成员到最新成员计（同 `in_flight_run_span_minutes`），单 pass 为 `0.0`；`execution_boundary` 缺失或非字符串读为 `None`，精确字符串相等匹配。
  - **校验**：下限 `MIN_ROOT_BLOCKED_MINUTES = 2 * MIN_INTER_PASS_MINUTES` 用 `_int_at_least`（「must be at least 10」）；上限沿用 in-flight 的「`<VAR>=<n> must be at most <reachable>`」措辞，`reachable = (scan_limit - HOUR_BUCKET_MARGIN - 1) * MIN_INTER_PASS_MINUTES`；新检查放在 in-flight 检查**之后**（既有 c1/c7/i17 精确 stderr 断言先看到自己的消息）。用例：`BASE_CONFIG` 下 9、5、16 拒绝、10 接受（扩展 `test_c1` 的 cases，证据不可读时拒绝表现为退出 2）；`(96, 415, ok)`、`(96, 416, refused)` 仿 `test_i17`；默认值 60 加进 `test_i0` 的 defaults tuple。
  - **测试 harness 允许改动（是 fixture 不是期望值）**：`BASE_CONFIG` 增加 `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES: "10"`（`SCAN_LIMIT=16` 的可达上限是 15，默认 60 会被拒）；该 env 名加进 `ALL_ENV_NAMES`；`SHIPPED_CONFIG` 增加 `"60"`。13 个 pass、60 分钟的 scenario 在 `SHIPPED_CONFIG`（scan 96）下跑，`BASE_CONFIG` 的搜索窗只有 4 个 pass。
  - **真实 artifact 用例**：仓库内没有 node-22 artifact，用 inline dict 并如实标注「按 writer 键集合重建」（`services/orchestrator/scheduler_runtime.py` ~:643-660）：`status: "preflight_blocked"`、`execution_boundary: scheduler_root_preflight_blocked`、2026-10-13 之后的 `started_at`/`finished_at`、`counts` 为 `scheduler_evidence_proofs.empty_counts()` 的 13 键全 0 字典、`lock.reason`、`root_preflight.status: "blocked"`、空候选列表、无 `progress_guard`；文件名 `scheduler_YYYYMMDDHH_<12hex>.json`，`--now` 取同期 10 月时间以免 `evidence_stale`/trigger-age 先触发；断言退出 1、verdict `scheduler_root_blocked`。
  - **其它 scenario 的构造**：「A non-root pass ends the run」——两组大小使得穿过 `db_free_runtime_preflight_blocked` 会越过阈值而仅新组不会，断言 `passes == 新组大小` 且 verdict `ok`；「stays neutral」——插入 root-blocked pass 前后比较 `signals` 与 `frontier`，允许差异只有 `neutral_count`、`no_submission_neutral_skipped` 各 +1 与新块本身；优先级可构造的三例：`timer_stopped`、`pass_limit_blocked` 压过，压过 `submission_stalled`（root-blocked pass 是 neutral，后面的 blocked streak 照常计数）；`lock_contended_persistent` 配对结构上不可达，不写测试（design D3 已记）。`_pass_payload` 增加 `execution_boundary=None` 关键字（默认不写该键），既有调用不变。
  - **receipt 块**：顶层 `root_blocked_run` 恰好四键 `{passes, span_minutes, threshold_minutes, newest_is_root_blocked}`，每个 verdict 都生成（`timer_stopped` scenario 仍记录），测试精确断言键集合；`RECEIPT_SCHEMA_VERSION` 保持 v1（只增不改）。退出码：`main()` 只对 `ok` 置 0，新 verdict 自动为 1。
  - **观察者/被观察面一致性（governing invariant）**：新增 parity 测试读取 `services/orchestrator/scheduler_runtime.py` 源文件并断言字面量 `scheduler_root_preflight_blocked` 存在于 `execution_boundary` 写点；`scripts/select_ci_tests.py` 中该 writer 的 stop rule 增加第二个 node id（同 `test_e5` 模式），`tests/test_select_ci_tests.py` 相应断言；记为 PR Boundary 偏离。
  - **文档**：`RUNBOOK_ANCHORS` 加一条；`stuck-detection.md` 的 `#####` 标题文本恰为 `stall-scheduler-root-blocked`，放在 `stall-lock-contended-persistent` 与 `stall-submission-stalled` 之间，内容写 env 名、receipt 块字段与处置（逐个检查 `NHMS_SCHEDULER_ALLOWED_ROOTS` 与 registry manifest 根的存在与挂载，指向 `node22-control-plane-manual-recovery.md`）；同文件「6.2.1 十一档 verdict」改「十二档」，「第 8–11 档」改「第 8–12 档」；锚由 `test_r2` 遍历 `RUNBOOK_ANCHORS` 校验。docstring：`classify_pass`（~:867）的「fully observed」句改为区分 pre-lock（`scheduler_root_preflight_blocked`、`scheduler_timing_level_unrecognised`，无 progress_guard，neutral）与 post-candidate；`grade` docstring 与测试模块 docstring 的「eleven」改「twelve」。
  - **selector**：探针 pin 行已存在；runbook 无需新行（`test_r2`/`test_i18` 随探针或其测试改动即跑）；只加上面的 parity node id。测试仍是单文件，新系列接在 `i` 系列之后，不分区。
  - **PR Boundary 偏离**：issue 写「探针 + 其测试 + runbook 一节」；本 task 还改 `scripts/select_ci_tests.py`（一个 node id）与 `tests/test_select_ci_tests.py`。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_stall_health.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py`；新增一个用例以 inline dict 重建 2026-10-13 之后真实 pass artifact 的字段子集（`execution_boundary: scheduler_root_preflight_blocked`、counts 全 0、无 `progress_guard`），断言退出码 1。

  **Suggested fixture level:** expanded - 告警 lane 的判定谓词与优先级改动；observer/observed 谓词一致性风险轴直接命中。

  **Minimal mergeable slice:** atomic - 谓词、阈值校验、verdict 槽位与 receipt 块是一条判定链；只加谓词不加 verdict 没有可观测结果。

## 3. node-22 单元失败告警（spec: node22-unit-failure-alert）

- [x] 3.1 新增 `infra/systemd/nhms-node22-unit-failure-alert@.service`（绝对路径）、`scripts/node22_unit_failure_alert_once.sh`、`infra/env/node22-unit-failure-alert.example`（恰好七个键，`NHMS_FRONTIER_SENDMAIL=/usr/sbin/sendmail`）；README authority 表补一行；`MANIFEST.json` 追加模板条目（第 20 条）；selector 登记。新增 `tests/test_node22_unit_failure_alert.py`。钉死的契约（fixture review 2026-10-17）：

  - **模板单元（恰好这些指令）**：`[Unit] Description=... %i`；`[Service] Type=oneshot`、`WorkingDirectory=/scratch/frd_muziyao/NWM`、`EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env`、`ExecStart=/scratch/frd_muziyao/NWM/scripts/node22_unit_failure_alert_once.sh %i`、`TimeoutStartSec=120`；无 `[Install]`、无 `UnsetEnvironment=`；头注释不得含字面 `%h/NWM`（即使作为与 node-27 的对比）。`-` 前缀是有意的：env 文件缺失时 handler 照常运行并 SKIPPED。
  - **example**：恰好七个非注释 `KEY=` 行（`^[A-Z_][A-Z0-9_]*=`）：`NHMS_FRONTIER_SENDMAIL=/usr/sbin/sendmail` 唯一带值；`NHMS_ALERT_EMAIL_TO`、`NHMS_ALERT_EMAIL_FROM`、`NHMS_SMTP_HOST`、`NHMS_SMTP_PORT`、`NHMS_SMTP_USER`、`NHMS_SMTP_PASS` 为空；不得复制 node-27 example 的真实收件人或 `REPLACE_ME` 密码；注释可说明 SMTP 键只供认证 SMTP shim 使用、node-22 本 change 不交付该 shim。真实 `.env` 只在 node-22，由既有 `infra/env/` ignore 规则覆盖。三个新文件与 README 行均不得出现字面 `DATABASE_URL`（拓扑硬门禁扫描 `scripts/`、`infra/env/`、`openspec/changes`；缺失断言只写在 `tests/`）。
  - **handler（与 node-27 的差异）**：`#!/usr/bin/env bash` + `set -euo pipefail`（node-22 的 `env` 禁令只针对 Python）；journal 固定 30 行：`journalctl --user -u "$UNIT" -n 30 --no-pager 2>&1 || true`，保留 "(no journal context available for ...)" 回退；去掉 `NHMS_UNIT_FAILURE_JOURNAL_LINES` 旋钮与 `JOURNAL_SCOPE=system` 分支；脚本内不给 `NHMS_FRONTIER_SENDMAIL` 默认值（默认只在 example）；绝不调用 `systemctl`；无 `LANG`/locale 处理（`charset="utf-8"` 头足够）；git 模式 `100755`。
  - **退出矩阵（镜像 node-27）**：缺 `$1`、`NHMS_ALERT_EMAIL_TO` 未设、`NHMS_ALERT_EMAIL_FROM` 未设、TO/FROM 含 CR/LF、`NHMS_FRONTIER_SENDMAIL` 未设或不可执行 → stderr `SKIPPED reason=...`、退出 0；发送用 `"$SENDMAIL" -t -i`；transport 非 0 用 `|| rc=$?` 捕获、记 `SEND_FAILED` 并以该 rc 退出；成功记 `SENT`。主题 `[NHMS node-22] unit failed: <unit>`，日志前缀 `node22-unit-failure-alert:`，hostname 缺省 `node-22`，保留 Date/MIME/`charset="utf-8"` 头。
  - **README 行（逐字）**：`| `nhms-node22-unit-failure-alert@.service` | `NWM/infra/env/node22-unit-failure-alert.env` | `node22-unit-failure-alert.example` |`，记为"未部署（见 Migration Plan）"而非"2026-09-02 实机核验"；`nhms-required-keys` 块不动；该节"five units in the table above"之类计数文字同步改正。
  - **manifest**：第 20 条 `{"deployed_relative_path": "nhms-node22-unit-failure-alert@.service", "tracked_path": "infra/systemd/nhms-node22-unit-failure-alert@.service", "sha256": ...}`，按字节序插在 `nhms-node22-scheduler-stall-health.timer` 之后、`nhms-scheduler-evidence-retention.service` 之前（0 起索引 9）；`tests/test_node22_unit_tracking.py` 与 drift 测试无需改动（可选：drift 测试 docstring 的 "nineteen"）。
  - **测试**：子进程 env 从零构建（不继承 `os.environ`），`PATH` = `tmp_path:` + 系统路径，放一个回显 argv 并打印哨兵行的假 `journalctl`；`NHMS_FRONTIER_SENDMAIL` 指向假 sendmail，它对每次调用**追加**一条记录（stdin + argv）以证明"恰好一封"；按路径执行脚本（缺 exec 位即失败）。断言：调用次数 == 1、argv == `-t -i`、正文含单元名、journal 哨兵与 journalctl argv `--user -u <unit> -n 30 --no-pager`；SKIPPED 用例退出 0、stderr 含 `SKIPPED`、捕获文件不存在；transport 退出 1 → handler 退出 1；静态断言脚本不含 `systemctl`；example 键集合精确；对 manifest 全部 20 个 `tracked_path` 断言不含字面子串 `%h/NWM`（不是裸 `%h`：三个 oneshot 合法使用 `%h/.local/bin`）；manifest 含模板条目且 sha256 等于文件（与 tracking 测试 (b) 有意重复，spec scenario 要求）；对模板跑 secret-value 扫描与 `EnvironmentFile=` authority 检查（去 `-`、拼 `/scratch/frd_muziyao/`、须是 README §#1694 表第 2 列反引号 token；tracking 测试只扫 `node22/`，顶层模板由本测试覆盖）。
  - **selector**：新增 3 行——模板单元 → (新测试, `tests/test_node22_unit_tracking.py`)；`scripts/node22_unit_failure_alert_once.sh` → (新测试)；`infra/env/node22-unit-failure-alert.example` → (新测试)；在精确的 `infra/env/README.md` 行与 `infra/systemd/node22/*` 行追加新测试；模板行不追加 drift 测试（与十二个 oneshot 行一致，master 全量回归兜底）；`tests/test_select_ci_tests.py` 更新 exact-set 字典并加 3 条「删掉即红」断言。
  - **PR Boundary 偏离**：issue 写「README/manifest/selector 各一处」，selector 实为 3 行 + 2 处追加；PR 偏离记录写明。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_node22_unit_tracking.py tests/test_node22_unit_drift_check.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py tests/test_env_templates.py`；`bash -n scripts/node22_unit_failure_alert_once.sh`。

  **Suggested fixture level:** compact - 复制已在 27 运行的模式到 22，改动面是路径、键集合与失败退出。

  **Minimal mergeable slice:** atomic - 模板单元、handler 与 example 三者缺一则无法被任何单元消费。

- [ ] 3.2 为 `infra/systemd/` 下六个 node-22 oneshot 单元各加一行 `OnFailure=nhms-node22-unit-failure-alert@%n.service`，同 PR 更新 `MANIFEST.json` 中这六个文件的 sha256；`tests/test_node22_unit_failure_alert.py` 增加 wiring 断言（恰好这六个各一行；gateway 单元与 `infra/systemd/node22/**` 为零；manifest sha256 一致）。钉死的契约（fixture review 2026-10-17）：

  - **行文本与位置**：逐字 `OnFailure=nhms-node22-unit-failure-alert@%n.service`（`%n` = 完整单元名含 `.service`，实例化后为 `nhms-node22-unit-failure-alert@nhms-x.service.service`，handler 的 `$1` 收到 `nhms-x.service`，与 node-27 消费者一致；不用 `%N`/`%i`/`%I`）。放在 `[Unit]` 节最后一个 directive 之后、分隔 `[Service]` 的空行之前：`nhms-scheduler-file-provider-refresh.service` 在 `Before=` 之后，`nhms-node22-scheduler-stall-health.service` 在 "declares NO relationship" 注释块之后，其余四个在 `Description=` 之后。不加注释行、不加空行，文件仍以单个 LF 结尾——六个文件的 diff 恰好各 +1 行。
  - **不接线**：六个 `.timer`、`nhms-compute-scheduler.service`（design Open Question Q1）、`nhms-compute-api.service`、`nhms-slurm-gateway.service`、`infra/systemd/node22/**` 与 3.1 模板本身都不加 `OnFailure=`。
  - **wiring 断言口径**：按节切片（参照 `tests/test_node27_raw_retention_canonical_deployment.py` 的 `_section` 写法），只数非注释行 strip 后与该行全等的次数，不用子串计数（模板与注释会提及该 directive）；`[Unit]` 内恰好 1、`[Service]` 内 0。"恰好这六个" = `infra/systemd/**` 下 `rglob` 到的全部文件（含 `.timer`、`.d/*.conf`、`MANIFEST.json`、3.1 模板、gateway 单元）中含该行的集合等于这六个 `.service`。manifest 断言只断言这六条的 `sha256` 等于文件内容的小写 hex（"同 PR 更新"由 `tests/test_node22_unit_tracking.py` 兜底）。
  - **manifest**：只替换六条的 `sha256`；条目顺序、2 空格缩进、结尾换行不变（3.1 加的第 20 条按字节序，不影响六条的位置）。
  - **文档漂移归本 task**：`scripts/node22_scheduler_stall_health.py` 模块 docstring 的 "A failed unit in the journal is this host's only alerting channel: node-22 carries no ``OnFailure=`` mail lane." 改为陈述 `OnFailure=` 已接到 `nhms-node22-unit-failure-alert@.service`；`docs/runbooks/production-ops/stuck-detection.md` "node-22 实测**没有**任何 `OnFailure=` 通道（node-27 才有 ... 接 mail 是独立一单" 一句改为已接线并指向 3.1 的 example/README 行。PR Boundary 偏离记录：多改一个 `.py` docstring 与一个 runbook 段落。
  - **selector**：把 `tests/test_node22_unit_failure_alert.py` 追加到六个 `.service` 行的测试元组（同 1.1/3.1 的 append 先例），`tests/test_select_ci_tests.py` 中钉住这些行精确集合的断言同 PR 更新；不追加的后果是以后只改 unit、删掉这一行并重算 manifest 的 PR 在 PR CI 上会绿。
  - **既有读者不变红（fixture review 已核实）**：stall 套件 u1 的 directive 元组不含 `OnFailure=` 且只抓含 `nhms-compute-scheduler` 的行；entrypoint invariant 的 exact-once 字段（`uv`/`python`/`enforce`/`EnvironmentFile=`/`ExecStart=`）不受影响；refresh installer 的 `UnsetEnvironment=` 字节相等与各处 `TimeoutStartSec` 正则不受影响；唯一变红的是 tracking 的 sha256 检查，同 PR 更新 manifest 即可。合并后到运维部署前 drift 探针对六条报 `differs` 是预期。
  - fixture level 保持 `none`：production config 触发项已由 design D4、Migration Plan 与 1.1 的 manifest 守卫消化；部署是带 receipt 的运维动作。模板 handler 不去重（stall 探针 15 分钟一趟，持续停摆约每天 96 封）是 D4 的既定选择，部署 receipt 应记录预期告警量。

  Depends on: 3.1, 1.1

  Verify：`uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_node22_unit_tracking.py tests/test_node22_unit_drift_check.py tests/test_node22_entrypoint_invariant.py tests/test_node22_entrypoint_invariant_python_scan.py tests/test_node22_scheduler_stall_health.py tests/test_scheduler_refresh_deployment_contract.py tests/test_node22_refresh_timer_health_installer.py tests/test_node22_refresh_timer_health_history.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py`；`git diff --stat` 显示六个 `.service` 各 +1 行。

  **Suggested fixture level:** none - 六行声明式配置加断言与 manifest 更新。

  **Minimal mergeable slice:** atomic - 六行同一语义，wiring 断言一次钉死集合；manifest 必须同 PR 否则 1.1 的 sha256 测试红。

## 4. env 卫生（spec: node22-env-hygiene）

- [ ] 4.1 `.gitignore` 增加 `.nhms-work/`；新增 `tests/test_gitignore_nhms_work.py` 用 `git check-ignore` 断言。钉死的契约（fixture review 2026-10-17）：

  - **位置与写法**：在 `.gitignore` 的 `.nhms-object-store/` 行（~:40）之后、`# Node-27 per-issue...` 注释之前插入 `.nhms-work/`（带尾斜杠），与 `.nhms-runs/`、`.nhms-workspace/`、`.nhms-object-store/` 并列。
  - **测试**：`REPO_ROOT = Path(__file__).resolve().parents[1]`；`subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "-v", <path>], cwd=REPO_ROOT)`，不在 git 仓库内直接失败、不 skip。正向：spec scenario 的嵌套路径 `.nhms-work/scheduler/basins-file-registry-publish/basins-inventory.json` 返回 0 且 `-v` 输出的 source 是 `.gitignore`、pattern 是 `.nhms-work/`（不钉行号；裸目录名 `.nhms-work` 不存在时 `check-ignore` 返回 1，所以必须用嵌套路径）。负向：同前缀兄弟 `.nhms-work.md` 与 `.nhms-workx/y` 返回 **1**（写 `== 1`，128 是 git 出错）；不要用已被忽略的 `.nhms-workspace/`。
  - **selector**：显式 non-goal——`.gitignore` 无 `PathTestRule` 也不在 ci.yml backend filter；本 PR 新增 `tests/**` 自然打开 backend lane 并选中新测试，以后只改 `.gitignore` 的 PR 靠 master 全量回归兜底。不加 selector 行、不加 ci.yml literal。
  - fixture level 保持 `none`：其它四个提到 `.gitignore` 的测试只在 `tmp_path` 写自己的文件；entropy 审计 `agent-artifact-ignore-policy` 只查固定 token。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_gitignore_nhms_work.py`；`git -c core.excludesFile=/dev/null check-ignore -v .nhms-work/scheduler/basins-file-registry-publish/basins-inventory.json` 输出 `.gitignore` 的 `.nhms-work/` 规则。

  **Suggested fixture level:** none - 一行 ignore 规则。

  **Minimal mergeable slice:** atomic - 单行。

- [ ] 4.2 `docs/runbooks/two-node-deployment-overview.md` §5.2：列表上方加退役句与完整六字段 section 标记，列表原样保留在标记区间内；新增 `tests/test_node22_runbook_section_markers.py` 用 `_section_archive_status_marker_ranges` 断言恰好一个完整 `section` 区间覆盖列表。钉死的契约（fixture review 2026-10-17）：

  - **版面（从上到下，相邻段之间各恰好一个空行）**：`### 5.2` 标题 → 退役句 → `22 使用：` → `Archive status:` 紧跟六行 `- key: value`（中间无空行；字段块后恰好一个空行，否则全角冒号的 `- Compose 文件：…` 会被当成 `retained_for` 续行吞掉）→ 原样保留的四行列表。先例版面：`docs/runbooks/node-27-database-container-operations.md:117-126`。不插入新的 `####` 标题截断区间：按解析器（`scripts/governance/entropy_audit/archive_status.py:45`、`_archive_status_section_end_line` ~:169）区间延伸到下一个任意级标题 `### 5.3`，因此覆盖 §5.2 剩余全部内容（Compose 形态列表与 writer DB / env roots / `NHMS_SCHEDULER_ALLOWED_ROOTS` 段），文件头 blockquote 已声明该段是设计意图描述，接受此结果。
  - **退役句原文**：`` `infra/env/compute.env` 是已退役的 docker-e2e 产物，node-22 没有任何单元加载它；单元到环境文件的权威见 [`infra/env/README.md`](../../infra/env/README.md#node-22-unit--environmentfile-authority-1694)。``（不含拓扑硬门禁敏感字样。）
  - **六字段（逐字）**：`Archive status:` / `- status: superseded` / `- current_authority: infra/env/README.md#node-22-unit--environmentfile-authority-1694` / `- superseded_by: docs/runbooks/node22-control-plane-manual-recovery.md` / `- status_since: 2026-10-16` / `- archive_scope: section` / `- retained_for: compose-lane design evidence`。
  - **测试断言**：`from scripts.governance.entropy_audit.archive_status import _section_archive_status_marker_ranges`（4.2 不受 1.1「stdlib only」约束）；`read_text(encoding="utf-8").splitlines()`；窗口取 `### 5.2` 到 `### 5.3` 之间；用四个完整字符串（`- Compose 文件：…`、`- 环境样例：…`、`- 本地真实环境文件：…`、`- systemd 样例：…`）定位列表并断言连续出现、与改动前逐字相同；逐字断言退役句与七行标记；覆盖列表行的区间恰好 1 个、起点落在 §5.2 窗口内的区间恰好 1 个（不断言全文件只有 1 个）；退役句、`Archive status:` 行、`### 5.2` 标题不在区间内；`end_line` == `### 5.3` 的 1 起行号 − 1；`start_line`/`end_line` 为 1 起闭区间。
  - **不动**：文件头 `最后更新：2026-06-22` 与顶部 blockquote（`tests/test_role_boundary_static.py:342` 钉住其中一句）；`archive_status.py`；`ENTROPY_AUDIT_TESTS`（`tests/test_select_ci_tests.py:3309-3330` 严格相等，重复 pattern 守卫在 :7753）——只改解析器的 PR 选不中新测试，记为已知限制。
  - **selector**：新增 `PathTestRule("docs/runbooks/two-node-deployment-overview.md", ("tests/test_node22_runbook_section_markers.py",))`（该 runbook 目前无任何行；注释注明 `docs/**` 不打开 backend lane），`tests/test_select_ci_tests.py` 加「删掉即红」断言；PR 偏离记录写明边界从「一个 runbook 节 + 一个测试」扩为含 selector 一行。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_runbook_section_markers.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py tests/test_role_boundary_static.py`。

  **Suggested fixture level:** compact - 文档标记加一个解析断言。

  **Minimal mergeable slice:** atomic - 标记与断言互为 oracle。

- [ ] 4.3 `infra/env/README.md` 在 required-key 块外新增 live-drift 分类表（七个键；status 三选一；「effective value when unset」按 loader 实际取值）；新增 `tests/test_node22_scheduler_env_drift_table.py`。钉死的契约（fixture review 2026-10-17）：

  - **三个 loader，逐行指定**：`NHMS_SCHEDULER_LEASE_GUARD_MODE` → `services/orchestrator/scheduler_lease.py` 的 `_file_lock_guard_mode()`（不是 `file_orchestration_journal.py` 里读 `JOURNAL_LOCK_GUARD_MODE` 的同名函数）；`NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS`、`NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`、`NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`、`NHMS_SCHEDULER_TIMING_LEVEL` → `services/orchestrator/scheduler_config/config.py` `ProductionSchedulerConfig` 各字段的 `default_factory`；`NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS`、`NHMS_SCHEDULER_EVIDENCE_MAX_MB` → `scripts/node22_scheduler_evidence_retention.py` 的 `config_from_env`（retention 单元使用，非 scheduler loader）。
  - **七行取值（测试逐行断言字面值）**：LEASE `optional, example differs (example: atomic, default: flock)`；RECONCILE_ABSENCE_SECONDS `optional (effective default: 300)`；IDENTITY_BLOCKED_STREAK_LIMIT `optional (effective default: 3)`；NO_PROGRESS_CIRCUIT_PASSES `optional (effective default: 3)`；TIMING_LEVEL `optional (effective default: stage)`；EVIDENCE_RETENTION_DAYS `optional (effective default: 90)`；EVIDENCE_MAX_MB `optional (effective default: 512)`。`required` 行为 0；键集合与 spec 七键精确相等、无重复。实现时若 loader 源码与此不符，以源码为准并在 PR 偏离记录说明。
  - **测试调 loader**：基线 = 当前进程环境只 `monkeypatch.delenv(key, raising=False)` 该键；LEASE 行同时删 legacy 键 `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` 后调 `_file_lock_guard_mode()` 期望 `flock`；四个 config 字段用 `dataclasses.fields(ProductionSchedulerConfig)` 取 field 并调 `default_factory()`（绕开 `__post_init__` 路径解析）；retention 用 `config_from_env(build_parser().parse_args(["--evidence-root", str(tmp_path)]))` 断言 `retention_days == 90`、`max_bytes == 512 * 1024 * 1024`。example 值用 `tests/test_env_templates.parse_env_template` 解析（截掉行尾 `#`）；断言 `optional (effective default: v)` 行的 example 值 == v，example differs 行的 example 值 == 表中值且 != default；status 与「effective value when unset」列一致。
  - **operator action 列**：只给不带注释的 `KEY=value` 或「保持未设置」，绝不写「从 example 复制」（systemd `EnvironmentFile=` 只认整行注释，行尾 `# ...` 会进值，TIMING_LEVEL 会让每趟 fail-closed；example 的三行行尾注释本身由 #2970 处理）；LEASE 行只提主键 `NHMS_SCHEDULER_LEASE_GUARD_MODE`，不设 legacy 键；本 change 不改 live 值（Non-Goals），LEASE 行动作是「记录、另行决策」并提示核对 live 文件是否带 legacy 键。
  - **runbook**：`required` 行为 0 → 4.3 不改 runbook；测试显式断言 required 集合为空，并加一个合成用例证明「required 行缺少 runbook `KEY=` 步骤时 helper 报违规」（不空跑）；PR 偏离记录写明「runbook 一节」缩为无。
  - **README 位置**：文件末尾追加 `## compute.scheduler-dbfree.env live-drift classification (2026-10-16)`，标准管道表 `| key | status | effective value when unset | operator action |`，键用反引号；测试按标题定位到下一个标题为止；避开 `nhms-required-keys` 块与 `- Forbidden env keys must match` ... `- Forbidden container/host surfaces:` 段（`tests/test_two_node_docker_runtime.py` 精确断言）；措辞不含拓扑硬门禁敏感字样。
  - **selector**：新测试追加到 `infra/env/README.md` 行、`infra/env/compute.scheduler-dbfree.env.example` 行（同步更新 `test_sibling_env_templates_keep_their_pinned_selections` 的精确列表）、`services/orchestrator/scheduler_lease.py` 行、`scripts/node22_scheduler_evidence_retention.py` 行；`services/orchestrator/scheduler_config/config.py` 无专属行 → 新开一行；新行加「删掉即红」断言；PR 偏离记录写明。
  - **与 4.4 的冲突**：两者都改 README 尾部与同一条 README selector 行；后合并者负责 rebase。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_scheduler_env_drift_table.py tests/test_env_templates.py tests/test_entropy_audit_report_contract.py tests/test_select_ci_tests.py tests/test_node22_unit_tracking.py tests/test_slurm_gateway_deployment_contract.py tests/test_two_node_docker_runtime.py`。

  **Suggested fixture level:** compact - 文档契约加一个对照 loader 的测试。

  **Minimal mergeable slice:** atomic - 七行分类是一张表，表与测试同生。

- [ ] 4.4 `infra/env/README.md` 新增「Backup copies」节；新增 `scripts/ops/node22_env_backup_sweep.sh`（`--env-dir`、`--systemd-user-dir`、`--backup-root`、`--home`、`--apply`）与 `tests/test_node22_env_backup_sweep.py` 覆盖 spec 四个 scenario；`node22-control-plane-manual-recovery.md` 补「先 dry-run 后 --apply、附 receipt」步骤；selector 登记。钉死的契约（fixture review 2026-10-17）：

  - **可移植性（本地 macOS 仍是 oracle）**：不用 `realpath -m`（macOS 无）、不用 bash 4 特性（`declare -A`、`mapfile`；本机 `/bin/bash` 3.2）、不用 `jq`/`python`/`rm `/`systemctl`；路径归一化用 `readlink -f`（父目录须存在；失败时退回「`%h` 展开后的原字符串」比较）；`set -euo pipefail`；`umask 077`。
  - **候选**：env 目录正下方、basename 匹配 `*.bak*`、`*.backup-*`、`*.pre-*`、且 `[ -f ] && [ ! -L ]` 的普通文件；不递归、不含点文件、`LC_ALL=C` 排序去重。
  - **拒绝集**：扫描 `--systemd-user-dir` 顶层 `nhms-*` 普通文件（任意后缀，含 `.service.pre-*` 残留，宁可多拒）与一层 `nhms-*.service.d/*.conf`；取非空 `EnvironmentFile=` 值（跳过 reset 行；`=` 两侧去空白；去一个前导 `-`；只展开 `%h` 为 `--home`）；两侧归一化后字符串相等即命中，stderr 点名单元相对路径，dry-run 也退出 2、零移动。fail-closed：`--env-dir`/`--systemd-user-dir` 不存在或不可读 → 2；扫到 0 个 `nhms-*` 单元 → 2（防目录写错导致空拒绝集）；`--backup-root/infra-env` 归一化后落在 env 目录或 systemd 目录之下 → 2。systemd 目录只读；`~/.config/systemd/user/` 下的 rollout 目录与 `disabled/` 不在范围（design「Not yet specified」）。
  - **apply**：第一次移动之前先对全部目标检查 `[ -e ] || [ -L ]`，任一已存在 → 2、零移动（`mv -n` 退出码不可靠，只作兜底）；每次 `mv -n` 后断言源已消失、目标存在，否则 2；目标目录 `infra-env/` 权限 0700（测试断言）。
  - **退出码只有 {0, 2}**：0 = dry-run 或零候选或 apply 成功；参数错误、拒绝、目标已存在、receipt 写失败、mv 失败 = 2。
  - **默认值**：`--backup-root` 默认 `$HOME/NWM-presync-backup-$(date -u +%Y-%m-%d)`（`--home` 只用于 `%h` 展开）；`--home` 默认 `$HOME`；`--systemd-user-dir` 默认同 1.2（`$XDG_CONFIG_HOME/systemd/user` 否则 `~/.config/systemd/user`）；`--env-dir` **必填**（避免漏传时扫开发机的 `infra/env/`）。
  - **plan 行**：每个候选一行 `plan: <env-dir>/<name> -> <backup-dir>/<name>`，测试按前缀计数。
  - **receipt**：`<backup-root>/infra-env/sweep-receipt-<UTC %Y%m%dT%H%M%SZ>.json`（dry-run 也 `mkdir -p` 该目录）；键集合精确为 `schema_version`（`nhms.node22.env_backup_sweep.v1`）、`generated_at`、`mode`（`dry_run|apply`）、`status`、`exit_code`、`env_dir`、`systemd_user_dir`、`home`、`backup_dir`、`candidates`、`moved`、`refusals[{candidate, unit}]`、`conflicts`、`errors`；只记文件名不记内容；bash 手写 JSON，转义 `"` 与 `\`，basename 含控制字符 → 2；receipt 已存在（同秒两次）→ 2 不覆盖；退出 2 时只要目录可建照样写 receipt。
  - **四个 scenario**（测试始终显式传全部路径参数，`["bash", script, ...]`）：dry-run——三个备份 + 一个 live 文件 → 三行 plan、receipt `candidates` 三项、文件都在、退出 0；拒绝——drop-in 写 `EnvironmentFile=-%h/NWM/infra/env/compute.env.bak-x`、`--home` 指向 tmp、env 目录 `<tmp>/NWM/infra/env` → 退出 2、stderr 含 drop-in 名、零移动；`--apply`——一个用例走默认 backup root（`env={"HOME": tmp, ...}`）断言目录名匹配 `NWM-presync-backup-\d{4}-\d{2}-\d{2}`、原名迁入、非匹配文件未动、receipt 存在、目录 0700；目标已存在——预置同名目标 → 退出 2、零移动、预置内容未变。静态断言：含 `set -euo pipefail`、`umask 077`；不含 `rm `、`systemctl`、`jq`、`python`、`realpath -m`、`declare -A`、`mapfile`；另跑 `bash -n`。测试同时断言 README「Backup copies」节写的默认路径与脚本默认值一致。
  - **README 节**：文件末尾（4.3 表之后）`## Backup copies`，prose/bullet 不用表格：备份绝不放 `infra/env/` 下，放 `~/NWM-presync-backup-<date>/infra-env/`，sweep 脚本用法与 receipt 位置；措辞不含拓扑硬门禁敏感字样。
  - **runbook**：放在 drift 探针节之后、`## 已知限制` 之前：`bash /scratch/frd_muziyao/NWM/scripts/ops/node22_env_backup_sweep.sh --env-dir /scratch/frd_muziyao/NWM/infra/env`，先 dry-run 再 `--apply`，receipt 附到 `docs/runbooks/receipts/`。
  - **selector**：新增 `PathTestRule("scripts/ops/node22_env_backup_sweep.sh", ("tests/test_node22_env_backup_sweep.py",))`（`.sh` 无同名推导）+「删掉即红」断言；README 行追加本测试（并更新成员守卫）；runbook 行不追加（无测试读新步骤）。
  - **与 4.3 的冲突**：同 README 尾部与 README selector 行；后合并者 rebase。
  - **PR Boundary 偏离**：selector 1 新行 + README 行追加。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node22_env_backup_sweep.py tests/test_select_ci_tests.py tests/test_entropy_audit_report_contract.py tests/test_env_templates.py tests/test_node22_unit_tracking.py tests/test_slurm_gateway_deployment_contract.py tests/test_two_node_docker_runtime.py`；`bash -n scripts/ops/node22_env_backup_sweep.sh`。

  **Suggested fixture level:** compact - 新增独立脚本，File IO 风险包（mv -n、拒绝集归一化）由测试覆盖。

  **Minimal mergeable slice:** atomic - dry-run、拒绝、apply 是同一候选集的三个出口，缺任一则脚本对运维不可用；策略一节是脚本目标目录的定义。
