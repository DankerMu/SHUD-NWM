# Design: node27-frontier-stall-alert（#1368）

## 风险三角与 fixture level

- 风险：**漏报**（告警器自身故障静默吞掉停摆信号 → 事故重演，本 issue 的存在理由）×**误报疲劳**（wall-clock 判据/无去重 → operator 忽略告警）×**凭据泄漏**（DSN 进邮件/日志/receipt）。
- fixture level：**expanded**（新生产自动化 lane + 持久化状态 + 外部副作用通道 + fail-safe 语义）；hand-written issue 无上游建议级别。修复强度：**high**（production config + secrets + 告警链即证据链）。

## 现状基线（fixture 撰写时核实）

- 全仓**无**邮件发送先例（grep sendmail/smtplib 零命中）——新能力，无可复用 helper。
- DB 访问先例：`scripts/node27_timeseries_retention.py:309`（`DATABASE_URL` env fail-closed）、`:1039-1043`（psycopg2 RealDictCursor）、`:1055-1064`（**`_redact_error_text` DSN 打码纪律**：psycopg2 异常会回显含明文密码的 conninfo，任何出错文本入日志/receipt 前必须打码——本脚本邮件正文同样适用）。
- `hydro.hydro_run`（`db/migrations/000006_hydro.sql:1`）：`source_id TEXT`、`cycle_time TIMESTAMPTZ` **nullable**——查询必须 `WHERE cycle_time IS NOT NULL`。
- systemd/env 模板成熟：8 个 `nhms-node27-*.timer` 先例（`infra/systemd/` 实数）。
- 治理注册约定：新 node-27 unit 必须进 `scripts/node27_resource_governance.py` `DEFAULT_SERVICES`（ADR 0002 尾注）。
- node-27 有只读角色 `nhms_display_ro`（role-level readonly）——本 lane 查询只读，**用只读 DSN**，最小权限。

## 决策

### D1 — 判据与状态机（progress-based）

观测：`SELECT COALESCE(source_id, '__null_source__') AS source_key, max(cycle_time) AS frontier, count(DISTINCT cycle_time) AS cycles, max(created_at) AS latest_created FROM hydro.hydro_run WHERE cycle_time IS NOT NULL AND status IN ('succeeded','parsed','published') GROUP BY 1 ORDER BY 1`。

- **`source_id` 可空**（000006 DDL）：COALESCE 到哨兵键 `__null_source__` 作为独立组——绝不丢行（丢行=该组推进不可见=漏报方向）；JSON state 键恒为字符串。
- **status 过滤 = post-ingest 生命周期集合 `('succeeded','parsed','published')`**（fixture 复审 round-2 P0 修正——单选 `succeeded` 会在生产稳态误报）：node-27 上 status 是同一 autopipe lane 内的单调三段推进——ingest 插 `'succeeded'`（`scripts/node27_ingest_run.py:187`，ON CONFLICT `:198-201` 保留既有 status）→ parse 升 `'parsed'`（`workers/output_parser/parser.py:889-902`，`node27_autopipeline.py:1421` 调用）→ publish 升 `'published'`（`node27_autopipeline.py:1098-1102`，未落 `river_timeseries` 的 run 永久停在 `parsed`）。集合内状态**跃迁不改变快照**（同 cycle 同行），无伪进度；`created/staged/submitted/running/pending`（非 node-27 写方；`pending` 系 000013 追加）与 `failed/cancelled/superseded` 不计。
- **进度是方向性的（严格增）**：progress = 新 source_key 出现，或某 source 的 frontier / cycles / latest_created **任一严格增大**。相等或**减少**（行转出集合：`failed/cancelled/superseded/pending` 家族跃迁；或人工 DBA 删除；source 整体消失同理）一律**不算进度**——减少若算进度，真实停摆期间一次 run 判 failed 就会重置 stall 时钟（漏报方向）。注：timeseries retention 物理上删不到 `hydro_run`（`node27_timeseries_retention.py:104-110` TARGET_HYPERTABLES 显式排除），不构成减少来源。`latest_created` 高水位覆盖回填期 count 相消几何（新 ingest 必然抬高 max(created_at)）。**基线只升不降**：非进度 tick 绝不下调持久化基线（per-source marker 取历史高水位）——否则 `succeeded→failed→parsed` 的出集合再入集合会伪造"严格增"重置时钟（`parser.py:32` PARSE_READY 含 failed，路径真实存在）。

- **进度定义**：见上（方向性严格增判据）。无任何 source 产生进度 → stall 计时继续。
- **告警条件**：`now - last_change_at >= stall_hours（默认 4h）`。阈值依据：全趟 2h16m-3h01m + 落库滞后 15-30min，上界 ~3h30m 留余量（issue 实测口径）。
- **状态机**：`ok --stall≥4h--> alerting --progress--> ok(发 recovered)`；`alerting` 内每 `resend_hours（默认 6h）` 重发。
- 状态持久化 JSON（`state_path`）：`{snapshot, last_change_at, alert_active, last_alert_at, consecutive_query_failures, schema_version, baseline_established_at, baseline_reset_at, last_degraded_alert_by_kind, last_error, baseline_pending, baseline_pending_kind, degraded_pending}`；**tmp+rename 原子写**。`last_error` 系实现期偏离 1 追认；`baseline_pending`/`degraded_pending` 系 review round-1 修正；round-2 修正（A/B 批裁定）：degraded 去重钟为 **per-kind**（`last_degraded_alert_by_kind: dict`，`schema_version` 不变）——单一家族钟会让一封 `state-corrupt` 跨类抑制首封 `observability-unavailable`（漏报方向，spec 与 design 冲突时 spec 胜出）；round-3 修正：legacy 标量 `last_degraded_alert_at` **不迁移**（标量无法归因到 kind，任何迁移语义都会重新引入跨类抑制=绝不允许列；不迁移的代价上限是一封重复邮件=允许的 over-report 方向），读入时按 bootstrap 处理（空钟）；`baseline_pending_kind`（`"bootstrap"|"reset"`，缺省按 bootstrap 兼容）让 pending 填充把戳记到正确字段（corrupt 起源填充记 `baseline_reset_at`，绝不冒充全新安装）。
- **基线只能由真实观测建立**（review round-1 A-C1 修正）：rebuild/bootstrap tick 若观测同时失败，**不得**落空基线、不得记 `baseline_established_at`/`baseline_reset_at`——否则下一次成功观测会把所有 source 判成"新 source"伪造进度，把 stall 时钟推迟整个 DB 中断时长（漏报方向）。此时置 `baseline_pending`，下一次成功观测**静默填充基线、不算进度、不动 `last_change_at`**（盲窗仍 ≤ D2 记账的 stall_hours 上界）。
- 时钟：注入式 `now()`（UTC），测试可拨。

### D2 — fail-safe 语义（宁可多报不可漏报，issue 硬要求）

| 故障 | 处置 | 绝不允许 |
|---|---|---|
| 状态文件**缺失**（首次 bootstrap / 换 state_path） | **静默**建基线：receipt 记 `baseline_established_at`，零邮件（否则每次全新安装必发一封，live receipt 事件归属说不清）。观测同时失败 → `baseline_pending`（不记 established，见 D1）。接受的权衡：state 被人为删除会伪装成 bootstrap，盲窗 ≤ stall_hours——监控连续性由 timer 装载 + 治理审计兜底，记录在案 | 把 bootstrap 当损坏发降级告警（噪音）；以空观测冒充已建基线 |
| 状态文件**损坏/schema_version 不符**（读/解析两阶段**整类收容**——`except Exception` 级，不再枚举异常类型；round-2 不变式审计裁定：round-1 枚举集漏 `OverflowError`（极值时间戳 astimezone）与 `MemoryError`，枚举修法与失败类不闭合） | 立即发 `monitoring-degraded` 告警，以当前观测重建基线并如实记 `baseline_reset_at`（pending 起源经 `baseline_pending_kind` 记到正确戳位）；观测同时失败 → `baseline_pending`。发送失败 → `degraded_pending` 落盘，下 tick 重试（受 per-kind 6h 去重钟约束）。**持久性损坏中 state_path 指向目录**一类（锁可开、tmp 写失败）→ 每 tick 发一封：over-report 方向、48 封/日上界，伴随每 tick exit 1 + unit failed；**锁文件本身不可 `O_RDWR`**（卷只读、root 遗留锁）则走下方锁行的 config error（rc=2 零邮件）——同族误配置的两种分叉行为均如实入 runbook，几何不对称的收口另立 issue | 静默重置 stall 计时（那是漏报方向）；state 读/解析任何异常类逃逸出收容 |
| DB 查询失败（连接与语句均**有界**：`connect_timeout` + `statement_timeout`——挂死不是失败、走不到本行，必须先被超时转化为失败） | 本 tick 记 `consecutive_query_failures += 1`；**不更新** `last_change_at`（stall 时钟照走）；连续 ≥2 tick（1h）发 `observability-unavailable` 告警（**per-kind** 6h 去重——绝不被 state-corrupt 家族钟跨类抑制；per-kind 钟下同类 6h 内的重复丢弃是合法去重，因为该类必已投递过） | 把查询失败当"无进度证据不足"而跳过计时；无界阻塞调用（监控器自身复现 11h 挂死几何）；跨类共享去重钟吞掉从未投递过的事件类 |
| sendmail 非零退出/binary 缺失 | 记入 receipt/日志（打码后），**不记** `last_alert_at` → 下 tick 必然重试。唯一例外：`frontier-recovered` 发送失败不重试、不重新武装告警（已不存在的停摆不该被重新武装；失败进 receipt/JSONL，exit 1 使 unit failed 可见——review round-1 C-C1 裁定，spec delta 已同步收口） | 记成"已告警"（吞掉重试） |
| 锁文件目录不可建/不可写（EACCES/EROFS/ENOTDIR） | 结构化 config error、退出非零（与 env 缺失同支，绝不裸 traceback） | 未映射异常类裸逃逸 |
| env 缺 `DATABASE_URL`/`NHMS_ALERT_EMAIL_TO` | 启动即结构化 config error、退出非零（systemd unit failed 可见） | 带默认收件人静默运行 |

### D3 — 邮件通道

- `subprocess` 调 `/usr/sbin/sendmail -t -i`（路径可配 `sendmail_path`，测试注入 fake 记录 invocation）。无 SMTP 凭据参与。
- 信头：`From: NHMS Frontier Alert <nwm@<hostname>>`（可配）、`To: $NHMS_ALERT_EMAIL_TO`、`Subject` 含事件类型与 stall 时长；正文含 per-source 观测明细（诊断用）、`last_change_at`、阈值、runbook 指引。`sendmail -t` 下收件人完全由信头决定：`NHMS_ALERT_EMAIL_TO`/`NHMS_ALERT_EMAIL_FROM` 含 CR/LF 即 config 期 fail-closed 拒绝（防头注入/误配静默跑偏）。
- **stalled 标签以投递事实为准**：`initial`/`resend` 由 `last_alert_at is None` 判定（与去重钟同源），不得用 `alert_active`——首封发送失败后的重试必须仍标 `initial`（operator 重建时间线依赖它）。
- **DSN/凭据零泄漏**：所有异常文本经打码（复用 retention `_redact_error_text` 模式，实现期抽公共 helper 或同构复制并注明出处）后才可进邮件/日志/receipt。
- 三类事件：`frontier-stalled` / `frontier-recovered` / `monitoring-degraded`（含 observability-unavailable 细类）。

### D4 — systemd / env / 治理

- `infra/systemd/nhms-node27-frontier-alert.{service,timer}`：`Type=oneshot`、`OnCalendar=*:00/30`、`EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env`；单实例互斥在 **Python 内**做（`fcntl.flock` 于 state 目录 lockfile，非阻塞，占用即结构化 no-op 退出 0）——wrapper `scripts/node27_frontier_stall_alert_once.sh` 保持薄壳；in-script 锁可被 pytest 直接钉（B13），wrapper 层锁先例（10 个 `*_once.sh` 中 3 个 flock + 1 个非 flock 锁）不可测。
- **本 unit 的 tick 必须有执行期限**（review round-1 B-C2 裁定）：`TimeoutStartSec` 设有界值（oneshot 的整个执行即 start 阶段；模板同族的 `TimeoutStartSec=0` 对秒级 tick 的监控 unit 不适用——挂死的监控器必须转成 unit failed 可见）。proposal Non-Goals 的 "RuntimeMaxUSec 兜底另开" 只覆盖 autopipe 业务 unit（阈值论证不可迁移），不豁免本 unit。
- **env 文件单读者**（review round-1 B-C3 裁定；round-2 修正）：systemd 部署路径以 `EnvironmentFile=` 为准，wrapper 仅在未注入时才 `source`（手工调试路径）；哨兵用 **lane-scoped 标记**（service 内 `Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1`），不用 `DATABASE_URL`（跨 lane 最共享的变量名，调试 shell 里他 lane 的 DSN 会误跳 source）。**symlink/0600 校验与"谁来 source"正交，必须无条件执行**（round-2 P2：1R.5 把校验一并关进 elif，systemd 路径上 644 明文口令文件被静默放行）；仅 `source` 本身留在哨兵门内。wrapper 必须有测试（兄弟先例 `tests/test_node27_timeseries_retention.py` wrapper 段），且必须含"已注入路径"用例。`.example` 值必须双语法安全（systemd 与 bash 同释义，含空格值加引号），并注明密码含 `` $ ` " \ `` 时两解析器分歧的风险。
- `infra/env/node27-frontier-alert.example`：`DATABASE_URL`（**只读角色 DSN**）、`NHMS_ALERT_EMAIL_TO`、`NHMS_ALERT_EMAIL_FROM`（可选）、`NHMS_FRONTIER_STALL_HOURS=4`、`NHMS_FRONTIER_RESEND_HOURS=6`、`NHMS_FRONTIER_STATE_PATH`、`NHMS_FRONTIER_RECEIPT_PATH`、`NHMS_FRONTIER_SENDMAIL=/usr/sbin/sendmail`、`NHMS_FRONTIER_QUERY_FAIL_TICKS=2`。
- `node27_resource_governance.py` `DEFAULT_SERVICES` 增补该 unit。
- 每 tick 原子覆写单文件观测 receipt（latest 语义）+ 追加式 alert 事件 JSONL——无正式 schema（Non-goal 记录在案）。

## Invariant Matrix

Governing invariant: 前沿连续 stall_hours 无推进 ⇒ 下一 tick 必然存在活跃告警（新发或重发窗口内）；告警器任何内部故障只能**提高**告警倾向、绝不降低；DSN/凭据在邮件、日志、receipt、state 中零出现。
Source-of-truth identity/contract: `hydro.hydro_run` 限 `status IN ('succeeded','parsed','published')` 行集的 per-source 三 marker 高水位基线 (frontier, cycles, latest_created) + state.json schema_version。
Surfaces:
- Producers: `scripts/node27_frontier_stall_alert.py`（观测、状态、邮件、receipt 唯一生产方）
- Validators/preflight: env 解析 fail-closed；state schema_version 校验
- Storage/cache/query: state.json 原子写；只读 DSN 查询
- Public routes/entrypoints: systemd timer + CLI `--once`/`--dry-run`（`--dry-run`=完整跑判定逻辑、打印本应发生的动作，**零副作用**：不发邮件、不写 state/receipt/JSONL）
- Frontend/downstream consumers: operator 邮箱（mumzy1995@163.com）；无代码消费方
- Failure paths/rollback/stale state: D2 全表（状态损坏/查询失败/发送失败）
- Evidence/audit/readiness: 观测 receipt + alert JSONL + node-27 live receipt
Regression rows:
- 4h 无方向性进度（含期间仅发生减少）→ 恰一封 stalled 邮件（fake sendmail 记录 1 次 invocation）
- 方向性进度（frontier/cycles/latest_created 任一严格增，或新 source）→ 零邮件，`last_change_at` 重置
- 减少（行转出集合/人工删除）→ 不算进度、基线不下调，时钟照走
- stall 持续 <6h 不重发、≥6h 重发；恢复 → 恰一封 recovered
- 状态损坏 → monitoring-degraded 邮件 + 基线重建记录，无异常逃逸
- 查询失败 tick → stall 时钟不重置；连续 2 tick → observability 邮件
- env 缺失 → 结构化 config error 退出非零、零邮件调用
- 任意故障注入路径的邮件/日志/receipt 文本 0 命中 DSN 密码子串
- 非 UTF-8/深嵌套 state 损坏 → 走 corrupt 分支（degraded 邮件 + 重建），零异常逃逸
- rebuild/bootstrap 与观测失败同 tick → `baseline_pending`，下一成功观测不判进度、不动 `last_change_at`
- degraded 发送失败 → `degraded_pending` 落盘，下 tick 重试成功
- 首封 stalled 发送失败后的重试 → 标签仍为 `initial`
- `NHMS_ALERT_EMAIL_TO`/`_FROM` 含 CR/LF → config 期拒绝
- psycopg2.connect 带有界 `connect_timeout`（钉常量/调用参数）
- 锁路径 EACCES/ENOTDIR → 结构化 config error（非裸 traceback）
- 极值时间戳（`9999-12-31T23:59:59-14:00`）state → corrupt 分支自愈，非逐 tick 死亡
- state-corrupt 邮件已发后的首次 observability-unavailable → 照发（per-kind 钟）
- corrupt 起源 pending 填充 → 记 `baseline_reset_at`（非 established）
- systemd 已注入路径下 wrapper 仍拒 644/symlink env 文件
- `NHMS_FRONTIER_STALL_HOURS` 为 nan/inf/超界 → config 期结构化拒绝
- 主机名派生 From 含禁字符 → sanitize 回落，不 reject（reject 是反方向）

## 边界面清单（high）

- 共享 helper 根：无既有共享面被改（新文件为主）；`DEFAULT_SERVICES` 是唯一触碰的既有代码点——加行不改语义
- 写面：state.json、receipt、JSONL——全部限于配置目录内、原子写、无删除面
- 发布/回滚面：无；停用=disable timer
- 陈旧态/幂等：state schema_version 防旧格式误读；重复 tick 幂等（fcntl 单实例锁 + 状态机）

## Risk packs

- Public API/CLI/script entry: selected——CLI/env 契约、`--once`/`--dry-run`
- Config/project setup: selected——env fail-closed、example 模板
- File IO/path safety/overwrite: selected——原子写、目录创建、无越界路径
- Schema/columns/units/field names: selected——hydro_run 列名/NULL 过滤钉
- Auth/permissions/secrets: selected——只读 DSN、打码纪律
- Concurrency/shared state/ordering: selected——flock 单实例、tick 幂等
- Error handling/rollback/partial outputs: selected——D2 全表
- Documentation/migration notes: selected——runbook 新节 + example
- Resource limits/large input/discovery: not selected——查询按 source 聚合，行数 ≤ source 数（个位数），无大输入面
- Legacy compatibility/examples: not selected——全新 lane，零既有消费方
- Release/packaging/dependency: not selected——零新依赖（psycopg2 在位；sendmail 是运行期外部 binary，缺失走 D2 结构化失败）

Domain packs（`openspec/project-profile.md`，逐个表态）：

- Published NHMS artifacts / display identity: selected——前沿=已发布产物身份，post-ingest 生命周期集合过滤 + 方向性进度判据即该 pack 的判据钉（D1，B2 含负例）
- Hydro-met time series / forcing windows: selected——`cycle_time` 语义（NULL 过滤、回填 cycles 计数即进度）在 B2 钉
- PostGIS/TimescaleDB domain behavior: selected——聚合查询走只读角色、无 hypertable 写面；B7 钉查询失败语义
- Slurm production lifecycle: not selected——本 lane 恰恰**不**观测 Slurm（被检测对象的任何故障类都坍缩为前沿不推进，这是 issue 选 node-27 落库产物做信号的理由）
- Geospatial/CRS: not selected——零几何面
- SHUD numerical: not selected——零数值面
- External providers: not selected——不触上游数据源；sendmail 外发可达性在 3.4 实测（proposal 待实测项）
- Run manifest / QC provenance: not selected——不读 manifest/QC，仅 hydro_run 聚合

## Evidence mapping

- AC 单测（判据/去重/recovered/持久化/fail-safe）→ tasks B1-B13 + 三轮复审回归锚 B14-B28（§2R/§2S/§2T）
- AC live receipt（timer 装载、真实 DB、真实邮件、拨钟触发）→ tasks 3.4（node-27 实机四步）
- AC ruff / openspec validate → tasks 3.2/3.3
