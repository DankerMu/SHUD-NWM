# Design: retention-pipeline-frontier-exemption (#1307)

## Context

`plan_retention`（`retention.py:246-284`）今天是纯函数：输入
`object_store_root`/`cutoff`/`published_artifact_root`，输出
planned/skipped；`run_retention`（`:310-336`）算 `cutoff = now - retention_days`
（`:323`）后执行删除，enabled/dry_run 双门控（env 默认 fail-safe：disabled +
dry_run）。调用链：pass 收尾 `scheduler_runtime.py:1387` →
`_run_retention`（`:1780-1818`，mixin 函数，只拿 `self` + `started_at`）→
`scheduler_core.py:243-255` bound-method forwarder。判据只有墙钟，不咨询任何
journal/候选状态。

调用点所在函数里，本趟 `candidates` / `blocked_candidates` /
`skipped_candidates` 已全部在内存（`scheduler_runtime.py:904-916` 构建，
`:1387` 处仍在作用域）。注意 evidence 块的
`cycle_window.start_time_utc`（`scheduler_evidence.py:213-214,238-243`）是
**未取整**的 `started_at - lag - lookback`，比真实发现窗口下界晚最多 2 个
source cycle 间隔——真实下界在 `scheduler_discovery.py:371-375`（双重
`_floor_to_source_cycle_boundary`，`scheduler.py:443-460`）。活跃下界可零
I/O 求得（fixture-review round-2 P1-1：地板必须按 discovery 同款公式重算，
不得取 evidence 块）。

## Decisions

### D1 — 判据：前沿豁免闸，下界 = min(候选态下界, 窗口地板)（fixture-review round-1 重裁）

采纳 issue 推荐方案（豁免闸）而非备选（cutoff 改按前沿计算）：备选在追赶期让
磁盘占用随落后程度线性膨胀，而 replay 正是磁盘最紧的时候。豁免闸只保护
`[active_lower_bound, ∞)` 一段，参数缺省即旧行为，可回滚。

活跃下界三源取 min（规约到 UTC-aware datetime）：

1. 本趟 `candidates`（selected）与 `blocked_candidates` 的 `cycle_time_utc`；
2. `skipped_candidates` 的 `cycle_time`，按**终态 reason 排除法**：仅排除显式
   终态集合 `{completed_duplicate_pipeline, terminal_hydro_success,
   terminal_completed_cycle, terminal_pipeline_success,
   duplicate_candidate_identity}`（`scheduler_candidates.py:385,:269`、
   `scheduler_state_decision.py:219,235,256` 经 `:1001/:1046` 透传）；其余
   **全部计入**——含 `active_slurm_job`（`scheduler_candidates.py:676,709`，
   生产 journal 路径 `:1030`）、`cancel_requested_active_slurm`
   （`:700,:1023`）、`active_slurm_status_sync_deferred`（`:686,:975`）、
   `active_slurm_status_sync_failed`（`:573,:877`）、
   `active_duplicate_pipeline`（`:358,:370`，生产 journal 路径
   `:1016/:1060`）等；**未知/新增 reason 默认落保护侧**（fail-safe：宁多留
   不误删，测试钉住）；
3. **窗口地板**：按 discovery 同款公式重算真实发现窗口下界——
   `_floor_to_source_cycle_boundary(_floor_to_source_cycle_boundary(
   started_at - lag, sources) - lookback, sources)`
   （`scheduler_discovery.py:371-375` 逐字同构；floor 实现
   `scheduler.py:443-461`，**网格来自配置 `allowed_cycle_hours_utc`**——
   默认与示例配置均为 `0,12` 即 12h（`scheduler.py:293`、
   `compute.example:67`），`{0,6,12,18}` 仅是 kwarg 为 None 时的 gfs/IFS
   回退，生产 config 归一化后不可达（PR round-2 RC2-D1 更正）；ERA5-only
   按天；sources 与网格 wiring 参照 `scheduler_core.py:510-512`，重算侧必
   须同样透传网格 kwarg（RC2-T1 钉住）。**不得**取 evidence 块的
   `cycle_window.start_time_utc`（无取整，比真实下界晚最多 2 个网格间隔，
   12h 网格下即最多 24h 未保护带——round-2 P1-1，带宽按 RC2-D1 更正）。

三源全不可得（无窗口且无候选态）→ `None`，纯墙钟——与现状一致。

**为何要窗口地板（fixture-review F3）**：backfill 模式只把 available 的最早
gap 选进 `source_cycles`，上游归档滚动导致的 `available=False` cycle 不产生任
何 candidate/blocked/skipped（`scheduler_discovery.py:492-500`）——其失败 run
工作区若只靠候选态下界会被删。窗口地板保护整个发现窗口范围，不依赖
availability/选中与否。候选态下界仍保留：防御候选经非 discovery 路径进入的
情形（min 恒 fail-safe）。第 3 级不再使用"本趟选中 cycle 的 min"（原稿措辞
错误：backfill 每趟只选 1 个、legacy 选最近若干，都不是窗口下界）。

**稳态漂移论证（fixture-review F1 修正，round-2 P1-1 再修，PR round-1 I-2(b)
实例化更正）**：地板含两次 floor 松弛，正确前置不变量是 `lookback_hours +
cycle_lag_hours + 2×max(source cycle interval) ≤ retention_days×24`；interval
由 `allowed_cycle_hours_utc` 网格决定——示例配置与 `scheduler.py:293` 默认均
为 `0,12` 即 12h（非早期行文假设的 6h）。示例配置
（`infra/env/compute.example:133,137,223`：lag=6、lookback=168、14d）满足
（168+6+2×12=198 ≤ 336），此时窗口地板与全部候选源恒晚于 cutoff、`min` 恒取
cutoff——逐 key 零漂移。**边界与越界配置**
（如 lookback=336+lag=6，或 `NHMS_RETENTION_DAYS` 被配小）下界早于 cutoff、
豁免闸咬合：漂移方向恒为 **fail-safe（只多留、绝不多删）**，且每一项豁免带
reason 进 receipt 可见。不声称 `MAX_LOOKBACK_HOURS=336` 提供代码级零漂移保证
（336 = 14×24，边界处不等式不成立）。原 compute.example 人工不变量的语义变
化：违反它不再导致产出→删除自旋，而表现为过度保留（磁盘增长）——不变量本身
仍应保持（D6 注释措辞据此改写）。

**live 事故与本设计的关系（评审残余风险 1 落档）**：node-22 receipt 显示
`2026072300` 在 2026-08-07（落后 ~16 天 > 336h）仍被逐趟重建 ⇒ 当时发现面确
实覆盖它 ⇒ 生产实配在越界域（或候选经非 discovery 路径进入）。两种情形分别被
窗口地板 / 候选态下界覆盖——自旋的两条可能成因路径都被闸住。实配数值本地不可
核，留待 D7 rollout receipt 反推核实。

失败方向：下界计算是内存 min()，无 I/O 无异常面；窗口块在每趟 pass 都构建，
追赶期不存在"静默拿到 None"的路径。

### D2 — run 工作区豁免走同一 cycle 级判据（窗口地板兜底）

`_collect_run_targets`（`:210-235`）经 `_extract_run_cycle` 把 `runs/<run_id>`
映射到 cycle_time，前沿判据同样生效。失败/恢复中 run 的 cycle 只要落在**真实
发现窗口**范围内（双重 floor 地板，无论 available 与否、选中与否）即被窗口地
板保护（F3 反例含窗口底部取整带一并闭合）；候选态源再覆盖非 discovery 进入的
情形。残余（既有墙钟行为，非本 change 引入）：cycle 已滑出发现窗口且老于
cutoff、但仍有活跃 Slurm job 的 run 工作区，三源都覆盖不到——除非该候选经非
discovery 路径进入内存态；此为现状语义的边界，不在本 change 收窄。不引入 per-run journal 查询：仓库无
`(run_id) -> 恢复中?` API（最接近的 `_job_needs_restart_reconcile` /
`_job_blocks_rollback_quiescence` 都是 journal-internal，
`file_orchestration_journal.py:7824-7865`），新建查询面超出 M 规模且被 cycle
级判据覆盖。老出窗口的失败 run 按墙钟 cutoff 保留 14 天后正常回收（现状语
义，issue 不要求永久保全）。

### D3 — 双解析器现状保留，不在本 change 统一

`retention.py:_extract_run_cycle`（宽松 token-split，`:146-152`）与 journal 的
`_FORECAST_RUN_ID_RE`（严格，`file_orchestration_journal.py:169`）是两个独立
维护的 run_id 解析器。本 change 判据只消费解析出的 cycle_time，不按 run_id 回
查 journal（D2）。宽松解析器的主风险方向是"多认 token → 多保护"（fail-safe）；
限定：若 run_id 含另一个 10 位数字 token，宽松解析可能取到**更早**时间戳而少
保护——概率低且属既有行为（既有 cutoff 判定同样受影响），统一双解析器属卫生
债，报告不修（Non-goals 路由）。

### D4 — receipt 形状、判定顺序与压缩存活

**判定顺序（fixture-review F6）**，cycle 目标与 run 目标一致：

1. `cycle_time >= cutoff` → skipped `within_retention_window`（现状）；
2. `bound is not None and cycle_time >= bound` → skipped
   `pipeline_frontier_exempt`；
3. 否则入选 planned。

豁免 reason 命名 `pipeline_frontier_exempt`（issue 原文用
`below_pipeline_frontier`，字面语义反了——"below frontier" 恰是可删侧；偏离
在 proposal/PR body 具名记录）。加入既有 reason 词表
（`static_asset_protected` / `unparseable_cycle_name` /
`within_retention_window` / `unparseable_run_cycle` / `protected_path`）。

**豁免项形状（fixture-review F5）**：只带 `{key, path, cycle_time, reason}`，
**不含 `size_bytes`**——豁免判定在 `_dir_size`（`:155-163`，全树 rglob+stat）
之前完成，被保护的恰是追赶期最大最热的目录（live 单 cycle forcing 352 MB +
canonical 488 MB），每趟 NFS 全树 stat 纯属白烧。既有两类 skipped 沿用现状形
状不动。

`RetentionResult.to_dict()` 新增顶层 `frontier` 块：

```json
"frontier": {
  "active_lower_bound": "2026-07-23T00:00:00+00:00" | null,
  "source": "candidates" | "skipped_in_flight" | "window_floor" | null,
  "protected_count": 17
}
```

`source` 记产生 min 的类别（并列时按 candidates > skipped_in_flight >
window_floor 优先记前者）。尺寸压缩（`scheduler_evidence_payload.py:626-650`
`_compact_retention`）现状把 skipped 明细剥成 `skipped_count`；`frontier` 块
是常数尺寸标量，加入 allowlist 原样保留（`_compact_mapping:717-720` 对
allowlist 键透传）。schema_version 不升：`frontier` 是纯新增可选键，retention
receipt 无 JSON Schema 约束（全仓仅 `retention.py:92` 与两处测试引用
schema_version 字符串），旧消费者零破坏。

**时区口径（fixture-review F8，事实已核定）**：`_parse_cycle_name` 返回
aware UTC（`retention.py:141` `.replace(tzinfo=UTC)`），`cutoff` aware
（`:323` `.astimezone(UTC)`）——两侧同构无需修。需要处理的是 skipped 源的
`cycle_time` 是**字符串**（`scheduler_types.py:105-106`，`_format_utc` 产出
`…Z` 形）：helper 用 `datetime.fromisoformat`（py≥3.11 直接解 `Z`）解析后规
约 UTC；解析失败的条目按 fail-safe 落保护侧不计入 min（等价于不收窄下界）——
不，解析失败无 cycle_time 则无从计入，正确语义是**跳过该条目**且不影响其他
源；测试钉住。

### D5 — pass 外删除面裁定：兄弟副本与 CLI cleanup 均 scope out + 路由/披露

**兄弟副本 `scripts/node27_raw_retention.py`**：同形墙钟判据（判据 `:160`，
执行 rmtree `:225`）但独立进程（systemd timer，非调度 pass 内），无
candidates/journal 上下文可达，且只删 `raw/`、跑在 node-27 车道（另一 worktree
在处理 node-27 issues）。口径裁定：**前沿判据适用但取值来源需另行设计**（脱离
pass 内存态，需 journal 直查或 evidence 文件消费），按 issue 原文"不必同 PR 落
地"路由 follow-up issue（合并前经 issue-scribe 立项）。

**CLI `cleanup` 入口（round-2 P2-3 补裁）**：`services/orchestrator/
cli.py:92-104` `_run_cleanup` 强制 `enabled=True` 直调 `run_retention`，无
pass 上下文。`active_lower_bound` 默认 `None` ⇒ CLI 签名与行为完全不变（向后
兼容），但**pass 外手跑 cleanup 无前沿保护**——追赶期运维手动 cleanup 仍可
重演删除。显式披露而非静默：与兄弟副本同一 follow-up issue 一并路由（两者同
属"pass 外删除面缺前沿取值来源"一类）。

### D6 — 签名线程化沿既有 mixin-forwarder 惯例

四处签名变更全部 keyword-only 带默认值（向后兼容）：
`plan_retention(..., active_lower_bound=None)` /
`run_retention(..., active_lower_bound=None)` /
`scheduler_runtime._run_retention(self, started_at, *, force_dry_run_reason,
active_lower_bound=None)` / `scheduler_core.py` forwarder 透传——与
`force_dry_run_reason` 已走的模式逐字同构（`scheduler_core.py:243-255` ↔
`scheduler_runtime.py:1780-1785`；既有测试以位置参数调
`scheduler._run_retention(NOW)`，keyword-only 默认值零破坏）。下界计算放
`scheduler_runtime.py` 私有 helper（消费 SchedulerCandidate/dict/窗口块），
`retention.py` 保持 scheduler-agnostic 纯函数（只收 datetime）。
`infra/env/compute.example:202-220` 注释改写措辞（对齐 F1）：不写"代码级已强
制"，写"违反不变量不再导致产出→删除自旋，改为表现为受控过度保留（receipt 可
见）——不变量仍须保持"。

### D7 — live AC 按 rollout 先例分离

issue AC6（node-22 实机连续两趟 pass 同 key 不再重复删除、前沿可推进）需要修复
已部署且追赶场景仍活跃——按 #1316→#1319 先例，本 PR 交付实现 + 本地全套证据，
live receipt 作为部署后兑现项路由 rollout issue（合并前 issue-scribe 立项，标
`oracle-blocked` + `node-22`），并在该 issue 里要求**反推核实实配**
（lag/lookback/retention_days，见 D1 live 事故推论）。不视为本 PR 的 merge 阻
塞项，PR body 偏离记录具名披露。

## Review focus

1. D1 三源 min + 终态排除法的保护完备性（尤其未知 reason 的 fail-safe 默认与
   窗口地板对 unavailable-gap 反例的覆盖）与稳态零漂移前置不变量的准确性。
2. D4 判定顺序（cutoff 先于 frontier）与两个 reason 的可区分性；豁免项不做
   `_dir_size` 的性能承诺是否真被实现兑现。
3. 稳态逐 key parity 测试的判别性（不是恒真）与边界配置（lookback=336）用例
   的豁免断言。
4. skipped 源字符串 cycle_time 解析的 fail-safe 语义。
