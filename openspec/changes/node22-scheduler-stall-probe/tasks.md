# Tasks

## 1. 探针本体 `scripts/node22_scheduler_stall_health.py`

- [ ] 1.1 **D4 自包含**：stdlib only。**禁止** `from services...` / `from packages...` 的任何 import
      （理由见 design「D4 自包含约束」）。文件名谓词与 `MAX_EVIDENCE_BYTES`、后缀元组在本文件内自带。
- [ ] 1.2 配置层：读 design 阈值表的全部 env 键；`--now` 只走 CLI 不进 env。
      **在收集任何证据之前**做范围校验（含 `scan_limit >= max(no_submission_passes, lock_passes) + 12`、
      两个 age 阈值 ≥240、`limit_lookback >= 15`、证据根是目录），任一不满足即结构化配置拒绝 + 退出码 2，
      且不读任何产物、不执行 systemctl。
- [ ] 1.3 systemd 证据：只跑 `systemctl --user show <unit> -p ...`（**只读**；本文件不得出现
      `start`/`stop`/`enable`/`disable`/`restart`/`reload` 任何变更动词，由 3.14 源码扫描守住）。
      读 timer 的 `UnitFileState`/`ActiveState`/`LastTriggerUSec`，service 的
      `UnitFileState`/`ActiveState`/`Result`。查询失败或字段不可解析 → `probe_failed`。
      **不设 next-elapse 档**：该 timer 是 `OnUnitActiveSec`，`NextElapseUSecRealtime` 恒空（见 design）。
- [ ] 1.4 有界枚举与读取：目录枚举上界 `max_entries_scanned`；**先过谓词 → 再剔
      `.pre_execution.json` → 最后取字典序最大的 `scan_limit` 个**（次序不可调换，理由见 design）；
      每次读 no-follow（拒软链、拒非普通文件）、≤ `MAX_EVIDENCE_BYTES`；
      超限/坏 JSON/缺 `started_at` 记为不可读 → `probe_failed`。
- [ ] 1.5 排序**只按内容 `started_at`** 降序。**不得**出现「`sorted(paths)` 结果直接进判级」
      或任何 `st_mtime` 读取参与判级。
- [ ] 1.6 三类 pass 口径（progress 打断 / blocked 延长 / idle 打断 / neutral 跳过），
      neutral 定义 = status ∈ {`lock_contended`,`preflight_blocked`,`resource_limit_blocked`,
      `restart_reconciled`} 或两个 counts 键任一缺失。**缺键不得读成 0，也不得整体 `probe_failed`。**
      中性趟数写进 receipt。
- [ ] 1.7 tracker：自解析 `no-progress-tracker.json`，断言
      `schema_version == "nhms.scheduler.no_progress_tracker.v1"`，不匹配即 `probe_failed`；
      **不 import** `scheduler_no_progress`（D4，且 `load_state(dir_fd)` 是写路径的一部分）。
- [ ] 1.8 抑制：`NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS` 逗号分隔 **reason 精确串**，
      只作用于 verdict 11；被抑制条目进 receipt 的 `suppressed[]`
      （subject_kind/subject_id/reason/consecutive_passes/matched_rule）。
      **不得**影响 verdict 1–10 任何一条。
- [ ] 1.9 判级：design 的 12 档固定优先级，首个匹配者胜；`ok` 仅在无任何条件匹配时可达；
      证据缺失**不得**回落到 `ok`。verdict 8 用时间窗口存在性（非「最新一趟」定点）。
- [ ] 1.10 receipt：bounded、原子写、自建 0700 receipt 根（在 `NHMS_SCHEDULER_STALL_RECEIPT_ROOT`，
      **不得**落在证据根下）。内容必须让判级可独立重推：verdict、退出码、
      **每个信号的观测值与其阈值并列**、三类 pass 计数、被抑制条目、不可读产物清单、
      `generated_at`、`schema_version`、`runbook` 指针。
- [ ] 1.11 非健康 verdict 额外向 stderr 打一行带 `runbook` 指针的结构化摘要（journal 是本机告警通道）。
- [ ] 1.12 探针**不写证据根、不改任何 systemd unit**（无自愈，detection only）。
- [ ] 1.13 退出码：`0` ok / `1` 告警 / `2` 配置拒绝。

## 2. systemd unit

- [ ] 2.1 `infra/systemd/nhms-node22-scheduler-stall-health.service`：`Type=oneshot`、
      `WorkingDirectory=/scratch/frd_muziyao/NWM`、`Environment=PATH=...`、
      `ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python <probe>`、
      `StandardOutput=journal`、`StandardError=journal`、
      `UnsetEnvironment=` 与 refresh 探针同一套 PG 变量全集、`UMask=0077`、`TimeoutStartSec=120`、
      **无 `PrivateTmp`**、**无 `EnvironmentFile`**（注释写明 drop-in retune 路径与理由）。
- [ ] 2.2 `.timer`：`OnCalendar=*:03/15`、`RandomizedDelaySec=60`、`Unit=` 指向 2.1。
      注释照 refresh timer 的纪律说清 `Persistent=` 不能复活「enabled 但 inactive」的探针，
      探针自身存活由 runbook 巡检行承担。
- [ ] 2.3 两个 unit 文件**都不得**出现 `nhms-compute-scheduler` 的任何
      `Wants/After/Requires/Before/BindsTo/PartOf`（被监视对象绝不进依赖图）。

## 3. 测试 `tests/test_node22_scheduler_stall_health.py`

> 诚实口径：新模块在 master 上「红」= 模块不存在，不算 red proof。oracle 是下列行为用例。
> systemd 用可注入的假 `systemctl` 可执行文件，不 monkeypatch `subprocess`。

- [ ] 3.1 11 档非健康 verdict 各一个用例 + `ok` 一个，每档断言 verdict **与退出码**。
- [ ] 3.2 优先级：systemd 死 + 产物 `resource_limit_blocked` → 报 systemd 档；
      `pass_limit_blocked` + circuit → 报前者；`evidence_stale` + `submission_stalled` → 报前者。
- [ ] 3.3 **「正在干活不得判成死」**：timer `ActiveState=inactive` 且 service `ActiveState=active`
      → **不**报 `timer_stopped`（193 分钟长 pass 的真实几何）。
- [ ] 3.4 `LastTriggerUSec` 超龄但 service 当前活跃 → **不**报 `scheduler_not_triggering`。
- [ ] 3.5 **乱序用例**：文件名字典序与 `started_at` 相反 → 判级取 `started_at` 最新那趟。
- [ ] 3.6 mtime 用例：最旧产物 mtime 改成最新 → verdict 不变。
- [ ] 3.7 **截断次序用例**：边界处 `X.json` 与 `X.pre_execution.json` 并存且
      `scan_limit` 恰好卡在边界 → 断言终态产物入窗（证伪「先截断后剔除」实现）。
- [ ] 3.8 三类口径：progress 打断 / blocked 延长 / idle 打断 各一用例；
      neutral（`lock_contended` 与 counts 缺键两种）**不打断也不延长** 各一用例。
- [ ] 3.9 counts 缺键不被读成 0：窗口内混入缺键的 `resource_limit_blocked` 产物，
      断言 `submission_stalled` 的 streak 计数未被它推进，且探针未整体 `probe_failed`。
- [ ] 3.10 `submission_stalled` 边界：恰好 N-1 趟 → 不报；恰好 N 趟 → 报；
      窗口内混入一趟 `submitted_count > 0` → 不报。
- [ ] 3.11 `pass_limit_blocked` 窗口性：`resource_limit_blocked` 那趟**不是**最新一趟但在回看窗口内
      → 仍报；落在窗口外 → 不报。
- [ ] 3.12 抑制异质用例：1 条被抑制慢性条目 + 1 条未抑制新条目 → `no_progress_circuit_open`；
      仅含被抑制条目 → `ok` 且 `suppressed[]` 非空。抑制不越界：被抑制 reason 存在时
      `evidence_stale` / `pass_limit_blocked` / `submission_stalled` 仍各自照报。
- [ ] 3.13 fail-closed：超 `MAX_EVIDENCE_BYTES`、软链、非普通文件、坏 JSON、缺 `started_at`、
      tracker schema 不符、systemctl 非零退出或输出不可解析、证据根无法列出
      → `probe_failed` 且非零退出，**绝不** `ok`。
- [ ] 3.14 **源码扫描**：探针源码不含任何 systemd 变更动词，且不含
      `from services` / `from packages` / `import services` / `import packages`（D4）。
- [ ] 3.15 **平价测试**：import `services.orchestrator.scheduler_evidence`，断言探针自带的谓词与
      `is_scheduler_pass_evidence_filename`（`:40`）对同一批名字逐一同判、
      常量与 `MAX_EVIDENCE_BYTES`（`:27`）、`SCHEDULER_PASS_EVIDENCE_SUFFIXES`（`:37`）一致。
      名字样本须含 `no-progress-tracker.json`、`repair_stale_*.json`、`stale-lock-clear-*.json`、
      `retention/` 子目录名（实机证据根里真实存在的无关条目）。
- [ ] 3.16 配置拒绝：范围外阈值、`scan_limit` 不足、证据根不是目录 → 退出码 2，
      且断言**未打开任何产物、未调用 systemctl**（用假 systemctl 的调用计数证明）。
- [ ] 3.17 receipt 可重推：断言 receipt 里每个信号的观测值与阈值成对出现，
      且 `runbook` 指针非空；断言 receipt **不在**证据根下。
- [ ] 3.18 探针不写证据根：跑完后断言证据目录条目集合与 mtime 集合不变。
- [ ] 3.19 unit 文件静态断言：无 `EnvironmentFile`、`UnsetEnvironment` 覆盖 PG 变量全集、
      `ExecStart` 指向 `.venv/bin/python`、有 `StandardOutput/StandardError=journal`、
      无 `PrivateTmp`、无 `nhms-compute-scheduler` 的任何依赖指令。
- [ ] 3.20 must-remain-green：`tests/test_node22_refresh_timer_health.py`、
      `tests/test_scheduler_evidence_retention.py`、`tests/test_scheduler_evidence_decidability.py`。

## 4. 文档（随本 PR 同一次 push，不得做成尾随 docs-only commit）

- [ ] 4.1 `docs/runbooks/production-ops/stuck-detection.md` 新增 §6.2：11 档 verdict 各一段处置，
      与探针 receipt 的 `runbook` 指针**互为对照**（锚点必须真实存在，由 4.6 核对）。
- [ ] 4.2 记录阈值 retune 的 drop-in 路径
      `~/.config/systemd/user/nhms-node22-scheduler-stall-health.service.d/10-thresholds.conf`。
- [ ] 4.3 记录安装步骤，以及**装前装后 `nhms-compute-scheduler.{timer,service}` 的
      `UnitFileState`/`ActiveState` 对拍**；同段**明写本单放弃了什么**：强制性、自动回滚、
      以及 `nhms-scheduler-file-provider-refresh.{timer,service}` 那对的保护
      （`install_node22_refresh_timer_health.sh` 的 protected-state 保护集是四个单元）。
- [ ] 4.4 巡检行：探针自身存活怎么看（`list-timers` 的 NEXT 不为 `-`、receipt 的 `generated_at` 新鲜）。
- [ ] 4.5 抑制白名单：当前值、**以及该 reason 为何结构不可收敛的出处**
      （Slurm accounting 不返回 `job_comment`，exact-comment 对账永远 unproven），并链到 #2570。
      不得只留代码注释里的裸断言。
- [ ] 4.6 核对 4.1 的锚点与探针里写死的 `runbook` 指针字符串完全一致（由 3.17 断言非空，此处人工核对指向）。

## 5. 验证

- [ ] 5.1 `uv run pytest -q tests/test_node22_scheduler_stall_health.py`
- [ ] 5.2 `uv run pytest -q tests/test_node22_refresh_timer_health.py tests/test_scheduler_evidence_retention.py tests/test_scheduler_evidence_decidability.py`
- [ ] 5.3 `uv run ruff check scripts/node22_scheduler_stall_health.py tests/test_node22_scheduler_stall_health.py`
- [ ] 5.4 `openspec validate node22-scheduler-stall-probe --strict --no-interactive`
- [ ] 5.5 `git diff --check`
- [ ] 5.6 **node-22 live receipt**：`git pull --ff-only` 后用
      `/scratch/frd_muziyao/NWM/.venv/bin/python scripts/node22_scheduler_stall_health.py`
      跑一次真实证据根（只读，无需装 unit），记录 verdict、退出码、receipt 路径与内容摘要；
      同时记录 `nhms-compute-scheduler.{timer,service}` 前后状态一致。
      **禁止** `uv sync` / 裸 `uv run`（维护窗口前的 node-22 纪律）。

## 风险包（selected / not selected）

- **Error handling / 部分输出**：selected —— 探针全部价值在于「读不到东西时不许说健康」，
  且 counts 缺键是实测存在的部分输出形状；由 1.6 + 1.9 + 3.9 + 3.13 覆盖。
- **Resource limits / 大输入**：selected —— 证据根实有 309 条且会长；有界枚举/有界读/单文件上界；
  由 1.4 + 3.13 覆盖。
- **Config / 项目设置**：selected —— 14 个 env 键 + 无 EnvironmentFile 的刻意设计 +
  范围校验先于取证；由 1.2 + 3.16 + 3.19 + 4.2 覆盖。
- **File IO / 路径安全**：selected —— no-follow 读、自建 0700 receipt 根、不写证据根；
  由 1.4 + 1.10 + 1.12 + 3.13 + 3.18 覆盖。
- **Legacy compatibility**：selected —— D4 自包含导致谓词与常量重复，必须被平价测试钉死，
  且不得改变既有分类；由 1.1 + 3.14 + 3.15 + 3.20 覆盖。
- **Concurrency / shared state / ordering**：**ordering 轴 selected** —— 排序与「剔除先于截断」
  是本单最易错处；由 1.4 + 1.5 + 3.5 + 3.6 + 3.7 覆盖。无并发与共享可变状态：探针只读，
  且刻意无自持 state（见 design 的白名单决策）。
- **Schema / 字段名**：not selected 为独立包，但 tracker `schema_version` 断言（1.7）、
  `counts.*` 与 systemd 字段口径均已由 node-22 实产物/实 `systemctl show` 核过并钉进 design。
- **Auth / 权限 / 机密**：not selected —— 探针不读任何凭据；unit `UnsetEnvironment` 全套 PG 变量是纵深防御。
- **Public API / CLI**：not selected —— 新 CLI 纯增量。
- **Release / 依赖**：not selected —— 无新依赖（D4 要求 stdlib only）。

## 非目标

- 邮件/IM 出口（node-22 实测无 `OnFailure=` 通道；接通是独立一单）。
- `services/orchestrator/monitoring.py` 的任何改动。
- `no_progress_circuit` 的 observe-only 语义与剥离次序（#1118）。
- 自愈（不启停、不清锁、不改 unit）。
- 安装脚本（放弃项已在 proposal 与 4.3 明写，不声称等价）。
- 「pass 挂死不落终态」与「长 pass 在飞」的进一步区分（需 `.pre_execution.json` 的
  `reserved_at`/`final_evidence_artifact` 语义，本单不建该档；systemd 侧信号已覆盖 lane 死亡）。
- #2570 A 组。
