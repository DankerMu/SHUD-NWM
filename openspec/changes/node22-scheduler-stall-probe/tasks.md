# Tasks

## 1. 探针本体 `scripts/node22_scheduler_stall_health.py`

- [x] 1.1 **D4 自包含**：stdlib only。**禁止** `from services...` / `from packages...` 的任何 import
      （理由见 design「D4 自包含约束」）。文件名谓词与 `MAX_EVIDENCE_BYTES`、后缀元组在本文件内自带。
- [x] 1.2 配置层：读 design 阈值表的全部 env 键；`--now` 只走 CLI 不进 env。
      **在收集任何证据之前**做范围校验（含 `scan_limit >= max(no_submission_passes, lock_passes) + 12`、
      两个 age 阈值 ≥240、`limit_lookback >= 30`（须覆盖周期 15 + `RandomizedDelaySec=60` + 余量）、证据根是目录），任一不满足即结构化配置拒绝 + 退出码 2，
      且不读任何产物、不执行 systemctl。
- [x] 1.3 systemd 证据：只跑 `systemctl --user show <unit> -p ...`（**只读**；本文件不得出现
      `start`/`stop`/`enable`/`disable`/`restart`/`reload` 任何变更动词，由 3.14 源码扫描守住）。
      读 timer 的 `UnitFileState`/`ActiveState`/`SubState`/`LastTriggerUSec`，
      service 的 `UnitFileState`/`ActiveState`/**`SubState`**/`Result`。
      查询失败或字段不可解析 → `probe_failed`。
      **不设 next-elapse 档**：该 timer 是 `OnUnitActiveSec`，`NextElapseUSecRealtime` 恒空（见 design）。
- [x] 1.3a **verdict 4 的 `Result` 口径**：`Result != "success"` 即触发，**不得**写成
      「属于某个失败值允许表」。systemd 对 oneshot 的取值域含
      `success`/`exit-code`/`signal`/`timeout`/`core-dump`/`resources`/`protocol`/`start-limit-hit`，
      本 session 只实测到 `success`（未观测到失败形态），所以必须 fail-closed 到「非 success 即报」。
- [x] 1.3b **「service 在跑」判据 = `ActiveState in {"active","activating"}`，SubState 测命名集合
      `{"dead","failed"}`（**不得**写成 `SubState != "dead"`：oneshot 失败后报 `failed`/`failed`，
      不等式会把它读成「在跑」并关掉 verdict 3 的闸门 —— Phase 1 抓到的 fixture 缺陷）。**
      调度器 service 是 `Type=oneshot`，在飞时实测为 `activating`/`start`，**永不 `active`**；
      写成 `== "active"` 会让 verdict 3/5/7 的护栏在健康长 pass 期间全部失效。
      verdict 3、5、**7** 都必须挂这道闸门（7 今天只靠 193.9 < 360 的数值侥幸成立，须改为结构成立）。
- [x] 1.4 有界枚举与读取：目录枚举上界 `max_entries_scanned`，**触顶即 `probe_failed`**
      （`os.scandir` 返回序任意，静默截断会推翻排序余量论证，见 design）；
      **先过谓词 → 再剔 `.pre_execution.json` → 最后取字典序最大的 `scan_limit` 个**（次序不可调换）；
      每次读 no-follow（拒软链、拒非普通文件）、≤ `MAX_EVIDENCE_BYTES`；
      超限/坏 JSON/缺 `started_at` 记为不可读 → `probe_failed`。
- [x] 1.5 排序**只按内容 `started_at`** 降序。**不得**出现「`sorted(paths)` 结果直接进判级」
      或任何 `st_mtime` 读取参与判级。
- [x] 1.6 四类 pass 口径（progress 打断 / blocked 延长 / idle 打断 / neutral 跳过）。
      **neutral 由写方派生的可观测量判定：产物无 `progress_guard` 键**
      （guard 在 `scheduler_runtime.py:834` 才构造，早退 pass 产物里没有该键 ——
      正是调度器自己「early-exit / pre-lock / lock-contended / resource-limit-aborted」四类的投影），
      **或** `counts.submitted_count` / `blocked_candidate_count` 任一缺失。
      **禁止用 status 允许表**：`restart_reconciled` 实测带 `blocked_candidate_count=47`
      （#2570 第一趟停摆），`preflight_blocked` 是 fully-observed，都必须按 counts 判。
      **缺键不得读成 0，也不得整体 `probe_failed`。** 中性趟数写进 receipt。
- [x] 1.7 tracker：自解析 `no-progress-tracker.json`，断言
      `schema_version == "nhms.scheduler.no_progress_tracker.v1"`，不匹配即 `probe_failed`；
      **不 import** `scheduler_no_progress`（D4，且 `load_state(dir_fd)` 是写路径的一部分）。
- [x] 1.8 抑制：`NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS` 逗号分隔 **reason 精确串**，
      只作用于 verdict 11；被抑制条目进 receipt 的 `suppressed[]`
      （subject_kind/subject_id/reason/consecutive_passes/matched_rule）。
      **不得**影响 verdict 1–10 任何一条。
- [x] 1.9 判级：design 的 12 档固定优先级，首个匹配者胜；`ok` 仅在无任何条件匹配时可达；
      证据缺失**不得**回落到 `ok`。verdict 8 用时间窗口存在性（非「最新一趟」定点），
      且其判定集合是**全部成功解析出的产物中落在回看窗内的那些**，不是 streak 的 20 趟子集；
      两个集合各自的参与趟数写进 receipt。
- [x] 1.10 receipt：bounded、原子写、自建 0700 receipt 根（在 `NHMS_SCHEDULER_STALL_RECEIPT_ROOT`，
      **不得**落在证据根下）。内容必须让判级可独立重推：verdict、退出码、
      **每个信号的观测值与其阈值并列**、三类 pass 计数、被抑制条目、不可读产物清单、
      `generated_at`、`schema_version`、`runbook` 指针。
- [x] 1.11 非健康 verdict 额外向 stderr 打一行带 `runbook` 指针的结构化摘要（journal 是本机告警通道）。
- [x] 1.12 探针**不写证据根、不改任何 systemd unit**（无自愈，detection only）。
- [x] 1.13 退出码：`0` ok / `1` 告警 / `2` 配置拒绝。

## 2. systemd unit

- [x] 2.1 `infra/systemd/nhms-node22-scheduler-stall-health.service`：`Type=oneshot`、
      `WorkingDirectory=/scratch/frd_muziyao/NWM`、`Environment=PATH=...`、
      `ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/NWM/scripts/node22_scheduler_stall_health.py`
      （绝对路径，照先例；unit 走部署树，D4 的 staging 收益只作用于手工运行）、
      `StandardOutput=journal`、`StandardError=journal`、
      `UnsetEnvironment=` 与 refresh 探针同一套 PG 变量全集、`UMask=0077`、`TimeoutStartSec=120`、
      **无 `PrivateTmp`**、**无 `EnvironmentFile`**（注释写明 drop-in retune 路径与理由）。
- [x] 2.2 `.timer`：`OnCalendar=*:03/15`、`RandomizedDelaySec=60`、`Unit=` 指向 2.1。
      注释照 refresh timer 的纪律说清 `Persistent=` 不能复活「enabled 但 inactive」的探针，
      探针自身存活由 runbook 巡检行承担。
- [x] 2.3 两个 unit 文件**都不得**出现 `nhms-compute-scheduler` 的任何
      `Wants/After/Requires/Before/BindsTo/PartOf`（被监视对象绝不进依赖图）。

## 3. 测试 `tests/test_node22_scheduler_stall_health.py`

> 诚实口径：新模块在 master 上「红」= 模块不存在，不算 red proof。oracle 是下列行为用例。
> systemd 用可注入的假 `systemctl` 可执行文件，不 monkeypatch `subprocess`。
> **harness 形状照抄先例**：`tests/test_node22_refresh_timer_health.py:56-89` 的
> `_write_fake_systemctl`（写一个记录每次调用的可执行 shim，`chmod(0o755)`，
> 经 `ENV_SYSTEMCTL` 注入路径）与 `:172-195` 的 `_run` 包装。
> **不要另起第二套 harness**；`:832` 的「只调用只读子命令」断言也照抄。

- [x] 3.1 11 档非健康 verdict 各一个用例 + `ok` 一个，每档断言 verdict **与退出码**。
- [x] 3.2 优先级：systemd 死 + 产物 `resource_limit_blocked` → 报 systemd 档；
      `pass_limit_blocked` + circuit → 报前者；`evidence_stale` + `submission_stalled` → 报前者。
- [x] 3.3 **「正在干活不得判成死」（用实测几何）**：timer `ActiveState=active`/`SubState=running`
      + service `ActiveState=activating`/`SubState=start`（`Type=oneshot` 在飞时的真实形态）
      → **不**报 `timer_stopped`。**不得**用 `service ActiveState=active` 造 fixture ——
      oneshot 永远不会是 `active`，那样会给 bug 盖绿章。
- [x] 3.4 同一在飞几何下，`LastTriggerUSec` 超龄 → **不**报 `scheduler_not_triggering`；
      最新终态产物超龄 → **不**报 `evidence_stale`。（两档共用 1.3b 的闸门。）
- [x] 3.4b `Result=exit-code` → `scheduler_service_failed` 且非零退出
      （证伪任何「失败值允许表」实现；实测未观测到失败形态，故用例必须自造）。
- [x] 3.5 **乱序用例**：文件名字典序与 `started_at` 相反 → 判级取 `started_at` 最新那趟。
- [x] 3.6 mtime 用例：最旧产物 mtime 改成最新 → verdict 不变。
- [x] 3.7 **截断次序用例**：边界处 `X.json` 与 `X.pre_execution.json` 并存且
      `scan_limit` 恰好卡在边界 → 断言终态产物入窗（证伪「先截断后剔除」实现）。
- [x] 3.8 四类口径：progress 打断 / blocked 延长 / idle 打断 各一用例；
      neutral（**无 `progress_guard` 键** 与 counts 缺键两种）**不打断也不延长** 各一用例。
- [x] 3.8b **回归用例（钉住 #2570 第一趟停摆的产物形状）**：`status=restart_reconciled`、
      **带** `progress_guard`、`submitted_count=0`、`blocked_candidate_count=47`
      → 必须判为 **blocked**（延长 streak），**不得**被判中性。
      同理 `preflight_blocked` + 带 guard + blocked>0 → blocked。
      （证伪任何「按 status 允许表判中性」的实现。）
- [x] 3.9 counts 缺键不被读成 0：窗口内混入缺键的 `resource_limit_blocked` 产物，
      断言 `submission_stalled` 的 streak 计数未被它推进，且探针未整体 `probe_failed`。
- [x] 3.10 `submission_stalled` 边界：恰好 N-1 趟 → 不报；恰好 N 趟 → 报；
      窗口内混入一趟 `submitted_count > 0` → 不报。
- [x] 3.11 `pass_limit_blocked` 窗口性：`resource_limit_blocked` 那趟**不是**最新一趟但在回看窗口内
      → 仍报；落在窗口外 → 不报。
- [x] 3.12 抑制异质用例：1 条被抑制慢性条目 + 1 条未抑制新条目 → `no_progress_circuit_open`；
      仅含被抑制条目 → `ok` 且 `suppressed[]` 非空。抑制不越界：被抑制 reason 存在时
      `evidence_stale` / `pass_limit_blocked` / `submission_stalled` 仍各自照报。
- [x] 3.13 fail-closed：超 `MAX_EVIDENCE_BYTES`、软链、非普通文件、坏 JSON、缺 `started_at`、
      tracker schema 不符、systemctl 非零退出或输出不可解析、证据根无法列出、
      **目录枚举触顶 `max_entries_scanned`**
      → `probe_failed` 且非零退出，**绝不** `ok`。
- [x] 3.14 **源码扫描**：探针源码不含任何 systemd 变更动词，且不含
      `from services` / `from packages` / `import services` / `import packages`（D4）。
- [x] 3.15 **平价测试**：import `services.orchestrator.scheduler_evidence`，断言探针自带的谓词与
      `is_scheduler_pass_evidence_filename`（`:40`）对同一批名字逐一同判、
      常量与 `MAX_EVIDENCE_BYTES`（`:27`）、`SCHEDULER_PASS_EVIDENCE_SUFFIXES`（`:37`）、
      **`SCHEDULER_PASS_EVIDENCE_PREFIX`（`:36`）** 一致。
      **必须断三个常量本身**，只断样本同判不够：谓词若将来收紧成「前缀 + 10 位 cycle +
      12 位 hex + 后缀」，样本仍会全部同判而探针拷贝静默偏松。
      名字样本须含 `no-progress-tracker.json`、`repair_stale_*.json`、`stale-lock-clear-*.json`、
      `retention/` 子目录名（实机证据根里真实存在的无关条目）。
- [x] 3.15b **CI 选测路由**：在 `scripts/select_ci_tests.py` 的 `PATH_TEST_RULES` 为
      `scripts/node22_scheduler_stall_health.py`、两个 unit 文件各立路由行
      （**测试套件本身不需要**：`tests/*.py` 在 `select_ci_tests.py:5366` 走自选中分支），**并把
      `services/orchestrator/scheduler_evidence.py` → 新套件** 这条边加上。
      照先例（`select_ci_tests.py:4068` / `:4345` 的「#2146 widened this row by one」、`:4365-4379`）。
      理由：importer 闭包只在改动路径本身是测试文件时生效（`select_ci_tests.py:5350-5366`），
      不加路由则 3.15 在「打破平价的那个 diff」上根本不会跑，只是 merge 后的事后探测器；
      且只动探针/unit 的 PR 会一条测试都选不出，降级成 `--collect-only` 零断言冒烟。
- [x] 3.15c 自测该路由：`uv run python scripts/select_ci_tests.py`（按其既有自测入口）
      对「改 `scheduler_evidence.py`」与「改探针」两种 diff 各验一次，断言新套件被选中。
- [x] 3.16 配置拒绝：范围外阈值、`scan_limit` 不足、证据根不是目录 → 退出码 2，
      且断言**未打开任何产物、未调用 systemctl**（用假 systemctl 的调用计数证明）。
- [x] 3.17 receipt 可重推：断言 receipt 里每个信号的观测值与阈值成对出现，
      且 `runbook` 指针非空；断言 receipt **不在**证据根下。
- [x] 3.18 探针不写证据根：跑完后断言证据目录条目集合与 mtime 集合不变。
- [x] 3.19 unit 文件静态断言：无 `EnvironmentFile`、`UnsetEnvironment` 覆盖 PG 变量全集、
      `ExecStart` 指向 `.venv/bin/python`、有 `StandardOutput/StandardError=journal`、
      无 `PrivateTmp`、无 `nhms-compute-scheduler` 的任何依赖指令。
- [x] 3.20 must-remain-green：`tests/test_node22_refresh_timer_health.py`、
      `tests/test_scheduler_evidence_retention.py`、`tests/test_scheduler_evidence_decidability.py`。

## 4. 文档（随本 PR 同一次 push，不得做成尾随 docs-only commit）

- [x] 4.1 `docs/runbooks/production-ops/stuck-detection.md` 新增 §6.2：11 档 verdict 各一段处置，
      与探针 receipt 的 `runbook` 指针**互为对照**（锚点必须真实存在，由 4.6 核对）。
- [x] 4.2 记录阈值 retune 的 drop-in 路径
      `~/.config/systemd/user/nhms-node22-scheduler-stall-health.service.d/10-thresholds.conf`。
- [x] 4.3 记录安装步骤，以及**装前装后 `nhms-compute-scheduler.{timer,service}` 的
      `UnitFileState`/`ActiveState` 对拍**；同段**明写本单放弃了什么**：强制性、自动回滚、
      以及 `nhms-scheduler-file-provider-refresh.{timer,service}` 那对的保护
      （`install_node22_refresh_timer_health.sh` 的 protected-state 保护集是四个单元）。
- [x] 4.4 巡检行：探针自身存活怎么看（`list-timers` 的 NEXT 不为 `-`、receipt 的 `generated_at` 新鲜）。
- [x] 4.5 抑制白名单：当前值、**以及该 reason 为何结构不可收敛的出处**
      （Slurm accounting 不返回 `job_comment`，exact-comment 对账永远 unproven），并链到 #2570。
      不得只留代码注释里的裸断言。
- [x] 4.6 核对 4.1 的锚点与探针里写死的 `runbook` 指针字符串完全一致（由 3.17 断言非空，此处人工核对指向）。

## 5. 验证

- [x] 5.1 `uv run pytest -q tests/test_node22_scheduler_stall_health.py`
- [x] 5.2 `uv run pytest -q tests/test_node22_refresh_timer_health.py tests/test_scheduler_evidence_retention.py tests/test_scheduler_evidence_decidability.py`
- [x] 5.3 `uv run ruff check scripts/node22_scheduler_stall_health.py tests/test_node22_scheduler_stall_health.py scripts/select_ci_tests.py`
- [x] 5.3b `uv run pytest -q tests/test_select_ci_tests.py`（选测路由改动的既有 oracle，已确认存在）
- [ ] 5.4 `openspec validate node22-scheduler-stall-probe --strict --no-interactive`
- [x] 5.5 `git diff --check`
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
- **Legacy compatibility**：selected —— D4 自包含导致谓词与常量重复，必须被平价测试钉死
  （含三个常量本身），且该测试必须**在 CI 里选得中**才算闸门；
  由 1.1 + 3.14 + 3.15 + 3.15b + 3.15c + 3.20 覆盖。
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

## 6. Phase 3 交叉评审修复（fix pass 1）

### 6.1 阻塞项

- [x] 6.1.1 **中性趟不得占用 streak 名额**（P1）。`blocked_streak` 当前遍历
      `records[:max(no_submission_passes, lock_passes)]`，默认 = 20 = 阈值本身，
      窗口里有一个 neutral，streak 上限即 19，`submission_stalled` **不可达**
      （live receipt 实测 neutral ≈ 1/16 趟）。改为遍历排序论证能担保的前缀
      `scan_limit - HOUR_BUCKET_MARGIN`（默认 52），跳过 neutral、遇 progress/idle 即停、
      数满 `no_submission_passes` 即判定。`lock_contended_streak` 语义不变，遍历范围同步。
      被跳过的 neutral 数写进 receipt。
- [x] 6.1.2 **tracker 契约副本缺 CI 路由**（P1）。`test_e3` 钉住探针自带的
      `TRACKER_SCHEMA_VERSION`/`TRACKER_FILENAME` 与 `scheduler_no_progress.py:54,56` 一致，
      但 `select_ci_tests.py` 没有这条边：改 `STATE_SCHEMA_VERSION` 的 PR 会绿着合并，
      合并后探针每 tick 判 `probe_failed`（优先级 1，盖住其余十档）。
      加 `PathTestRule("services/orchestrator/scheduler_no_progress.py", (<新套件>,))`。
- [x] 6.1.3 **Markdown Lint 挂**：runbook §6.2 的 12 处 `<a id="stall-…"></a>` 触发 MD033，
      仓库 `.markdownlint.yaml` 只放行 `br`/`sup`/`sub`，全仓无第二处内联 HTML。
      改为标题锚点（标题文本即 anchor id，如 `##### stall-probe-failed`），
      **不要**改 `.markdownlint.yaml` 的全仓策略；同步更新 `test_r2` 的锚点断言方式，
      探针的 `RUNBOOK_*` 常量字符串保持不变。

### 6.2 非阻塞但本轮一并修

- [x] 6.2.1 guard 在场的 `resource_limit_blocked` 被判 idle 而打断 streak
      （`scheduler_runtime.py:1505-1527` 写零 counts 且可能附 guard）。
      neutral 增加第三条：`status == "resource_limit_blocked"`。加用例：
      该形状放在 lookback 之外、blocked 序列之中，断言不打断 streak。
- [x] 6.2.2 路由行缺回归钉子：照先例 `tests/test_select_ci_tests.py:406-461` 的
      `NODE22_REFRESH_READER_EDGES`，为本单全部 5 条 edge 加「存在」+「删掉即红」断言。
      （偏离：`scripts/node22_scheduler_stall_health.py` 那条是 pin，同名派生
      `scripts/<x>.py -> tests/test_<x>.py` 无行也能选中，故「删掉即红」对它不成立；
      补集用例对其余 4 条断言变暗、对这条断言**仍亮**，派生失效时即红。）
- [x] 6.2.3 runbook §6.2.2 把 `daemon-reexec` 改为**只写 `daemon-reload`**：
      reexec 会重启托管生产调度器的 user manager，而同节 `:213` 正把
      「daemon 被重新执行过」列为 `scheduler_not_triggering` 的疑似成因 ——
      调阈值的步骤不能是它所调告警的成因。
- [x] 6.2.4 抑制白名单无法经 drop-in 清空：`source.get(name) or default`
      会把 `Environment=NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS=` 静默还原成默认值。
      该键须区分「未设置」与「显式空」。这是唯一一个作用为压告警的配置项。
- [x] 6.2.5 runbook §6.2.4 抑制理由的出处**引错了**：引的 §6.1 第三行是另一个字符串
      `query_unavailable:comment_accounting_unproven`，且那条的结论是「必须人工处置」。
      改为引 `docs/runbooks/failed-basin-retry.md:343-350` / `:459-477`，
      并把措辞从「结构上不可收敛」改为「**不能自动收敛**，处置走有保护的运维动作」；
      同节补一句：`suppressed[]` 里的条目应走人工处置流程，不是可以无视。
- [x] 6.2.6 verdict 8 不可操作：runbook `:241` 让运维「打开 receipt 里对应的那趟产物」，
      但 `signals.resource_limit_passes_in_window` 只是整数。receipt 增记窗口内那几趟的
      **有界文件名列表**（兼顾 1.10「判级可独立重推」）。
- [x] 6.2.7 receipt 根校验不对称：证据根用 `realpath`、receipt 根用 `abspath`
      （`:444-446`），经软链指进证据根会被放行。两处统一。
- [x] 6.2.8 §6.2.3 的装后对拍只 diff `Id,UnitFileState`：`ActiveState`/`SubState`
      会随 pass 起落变化，含进去则 `PROTECTED_UNCHANGED` 无法机器判定；其余值单独记录。
- [x] 6.2.9 scheduler timer 的 unit 文件缺失时 `UnitFileState` 为空 → 现在判 `probe_failed`
      且 runbook 指针指错段落。至少让该 tick 的 runbook 指针指向 verdict 2 的处置段，
      或在 §6.2.1 补一句「`UnitFileState` 为空通常意味着 unit 文件被删」。
      （取后者：§6.2.1 第 1 档补句并指向第 2 档恢复段；判级仍 fail-closed 为
      `probe_failed`，与 spec「缺失属性即不完整证据」一致。node-22 实测
      `systemctl --user show <不存在>.timer` 退出 0、`LoadState=not-found`、`UnitFileState=` 为空。）

### 6.3 验证

- [x] 6.3.1 `uv run pytest -q tests/test_node22_scheduler_stall_health.py tests/test_select_ci_tests.py`
- [x] 6.3.2 `uv run ruff check scripts/node22_scheduler_stall_health.py tests/test_node22_scheduler_stall_health.py tests/test_select_ci_tests.py scripts/select_ci_tests.py`
- [x] 6.3.3 `npx markdownlint-cli2 "docs/runbooks/production-ops/stuck-detection.md"`（或仓库 CI 同款调用）
- [x] 6.3.4 定向变异自证：把 6.1.1 改回 `records[:window]` → 新用例必须红；
      去掉 6.1.2 的路由行 → 6.2.2 的补集断言必须红。
