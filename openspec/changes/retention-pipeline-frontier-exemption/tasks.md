# Tasks: retention-pipeline-frontier-exemption (#1307)

Fixture level: expanded（issue 无 Suggested fixture level 字段；规模 M、
priority:high 生产自动化缺陷、判据+线程化+证据面三段改动）· Repair intensity:
high · Seams under test：`plan_retention` 判据（纯函数 seam，含
`active_lower_bound` 判定顺序与 None 退化）、`_run_retention` 线程化
（scheduler_runtime → scheduler_core forwarder → retention）、活跃下界计算
helper（candidates/blocked ∪ skipped 终态排除法 ∪ 窗口地板三源 min）、receipt
`frontier` 块与 `pipeline_frontier_exempt` skipped 明细、`_compact_retention`
压缩存活——由 issue 复核命令 `pytest -k retention` 声明；无新 seam。

## Risk packs (considered)

- File IO / path safety / overwrite: **selected** — 删除目标选取判据本体；
  fail-safe 方向"多保护"（未知 reason 落保护侧、宽松 run 解析器多认即多豁
  免——D3 记录了低概率反向限定）。
- Error handling / rollback / partial outputs: **selected** — retention 永不
  中断 pass 的既有契约保持；None 退化路径、skipped 源字符串解析失败的
  fail-safe 语义（design D1/D4）。
- Schema / columns / units / field names: **selected** — receipt `frontier`
  块、`pipeline_frontier_exempt` reason 词表新增（与 issue 用词
  `below_pipeline_frontier` 的具名偏离）、豁免项无 `size_bytes` 的形状差异、
  压缩 allowlist、schema_version 不升的兼容论证。
- Legacy compatibility / examples: **selected** — 稳态逐 key 零漂移（AC3，
  前置不变量 `lookback+lag+max_interval ≤ retention_days×24`）、enabled/
  dry_run 门控与 forced-dry-run 语义不变、`PROTECTED_PREFIXES` 不动、既有
  skip reason 全保留、既有 retention 测试零回归。
- Resource limits / large input / discovery: **selected**（fixture-review 升
  选）— 豁免判定必须先于 `_dir_size` 全树 rglob+stat（追赶期被保护目录最大
  最热，每趟 NFS 全树 stat 白烧）；过度保护的磁盘风险 D1 论证有界且 receipt
  可见。
- Documentation / migration notes: **selected**（窄）— `infra/env/
  compute.example:202-212` 不变量注释按 D6 措辞改写（不得写"代码级已强制"）。
- Concurrency / shared state / ordering: not selected — 纯 planning，单 pass
  内内存态求 min，无共享态。
- Public API / CLI / script entry: not selected — keyword-only 默认值使
  `services/orchestrator/cli.py:92-104` `cleanup` 入口签名与行为完全不变；
  该入口 pass 外无前沿保护的事实由 D5 显式披露并随 follow-up 路由（round-2
  P2-3），不在本 change 修。
- Auth / permissions / secrets: not selected — 无凭据面。
- Config / project setup: not selected — 不新增 env/config。
- Release / packaging / dependency compatibility: not selected —
  `fromisoformat` 解 `Z` 需 py≥3.11，`pyproject.toml:9` requires-python
  已 ≥3.11，无跨版本面。
- Domain packs: not selected — 无数值/地理面。

## Tasks

- [x] 1. `retention.py`：`plan_retention` / `run_retention` 新增 keyword-only
  `active_lower_bound: datetime | None = None`；cycle 目标与 run 目标统一按
  **两级判定顺序**：① `cycle_time >= cutoff` → skipped
  `within_retention_window`（现状）；② `bound 非 None 且 cycle_time >= bound`
  → skipped `pipeline_frontier_exempt`，条目只带
  `{key, path, cycle_time, reason}` **不含 size_bytes**（豁免判定先于
  `_dir_size`，被豁免目录零 rglob/stat）；③ 否则入选。
  `RetentionResult.to_dict()` 新增 `frontier` 块
  （active_lower_bound/source/protected_count，D4 形状）。
- [x] 2. `scheduler_runtime.py`：新增私有 helper 计算活跃下界，三源取 min：
  (a) candidates ∪ blocked_candidates 的 `cycle_time_utc`；(b)
  skipped_candidates 的 `cycle_time`（字符串，`fromisoformat` 解析规约 UTC，
  解析失败条目跳过），按终态排除法——排除集合
  `{completed_duplicate_pipeline, terminal_hydro_success,
  terminal_completed_cycle, terminal_pipeline_success,
  duplicate_candidate_identity}`，**其余含未知 reason 全部计入**；(c) 窗口地
  板按 discovery 同款双重 floor 公式重算
  （`_floor_to_source_cycle_boundary(_floor_to_source_cycle_boundary(
  started_at - lag, sources) - lookback, sources)`，
  `scheduler_discovery.py:371-375` 同构；**禁止**取 evidence 块的未取整
  `cycle_window.start_time_utc`）。返回
  `(bound, source)`，source ∈ {candidates, skipped_in_flight, window_floor,
  None}（并列取前者）。`:1387` 调用点与 `_run_retention`（`:1780-1818`）线程
  化传参；`scheduler_core.py:243-255` forwarder 透传——keyword-only 带默认，
  与 `force_dry_run_reason` 模式同构。
- [x] 3. `scheduler_evidence_payload.py` `_compact_retention`（`:626-650`）
  allowlist 补 `frontier` 标量块，压缩态原样保留。
- [x] 4. 测试（`tests/test_retention.py` 纯函数/集成 + `tests/
  test_production_scheduler.py` 压缩 pin）：
  - 追赶豁免（AC1）：cycle C 的 forcing/ + runs/ 老于 cutoff、bound ≤ C →
    不选中，skipped reason 为 `pipeline_frontier_exempt`（**与
    `within_retention_window` 可区分**：同测试内放一个未到期 cycle 断言两
    reason 并存各归各）；frontier 块字段齐全可判读；
  - 终结反向（AC2）：C 老于 cutoff 且 bound > C（候选全终结）→ 照删，
    freed_bytes 口径不变；
  - 稳态 parity（AC3）：**钉满足不变量的配置**（lookback=168/lag=6/
    retention 14d）下带 bound 与不带 bound 计划逐 key 一致；判别性对照：同场
    景把 bound 人为压老须能改变计划（防恒真）；
  - **边界配置用例**（fixture-review F1，round-2 P2-4 具体化）：
    lookback=336/lag=6/retention 14d 时豁免带 `[bound, cutoff)` 仅 ~6-12h
    宽——在带内种 cycle 目录（如 `started_at-338h` 向下取整到 source cycle
    边界）断言被豁免记 `pipeline_frontier_exempt`；**同时**种一个 `< bound`
    的 cycle 断言仍入选 planned（双向断言防恒真）；
  - **pass 级端到端线程化**（round-2 P1-2，进 test_production_scheduler）：
    跑 `run_once`，构造老于 cutoff 且带非终态候选的 cycle，断言
    `evidence["retention"]["frontier"]["active_lower_bound"]` 非 null、
    `source` 在词表内、该 cycle key 出现在 `skipped`
    （reason=`pipeline_frontier_exempt`）且不在 `planned`/`deleted`——证明
    `:1387` 调用点真的算出并传入了 bound（forwarder 线一并覆盖）；
  - **窗口地板公式钉**（round-2 P1-1）：helper 单测——`sources=["gfs"]`、
    `started_at` 取非边界时刻（如 03:17Z），断言 `window_floor` 等于
    discovery 同款双重 floor 算式结果；plan 级用例：在
    `[discovery_floor, evidence_floor)` 取整带内种无候选 cycle，断言
    `pipeline_frontier_exempt` 而非入选；
  - 失败 run 工作区豁免（AC4）：老于 cutoff 但 ≥ bound 的 `runs/<run_id>` 不
    删，工作区内 `shud_stdout.log`/`shud_stderr.log` 事后可读；
  - helper 三源语义：终态集合条目不计入 min；`active_slurm_job` 等 in-flight
    条目计入；**未知 reason 计入（fail-safe 钉住）**；三源全空 → 窗口地板；
    窗口也不可得 → None（纯墙钟，与现状断言一致）；skipped 字符串
    cycle_time 解析失败条目跳过不抛；
  - 豁免项形状：无 `size_bytes` 键（并以此佐证未做 `_dir_size`）；
  - 压缩存活（设计派生要求 D4，非 issue AC）：尺寸压缩后 `frontier` 块保留、skipped 明细照旧剥
    为计数（既有 pin 适配不弱化）。
- [x] 5. `infra/env/compute.example:202-212` 注释按 D6 措辞改写：保留数值示
  例与不变量本身，改述后果为"违反不再导致产出→删除自旋，表现为受控过度保留
  （receipt frontier 块可见）"。
- [x] 6. 既有测试零回归：`uv run pytest -q tests/test_retention.py` 与
  `uv run pytest -q tests/test_production_scheduler.py -k retention` 全绿且断
  言不弱化；受签名影响的直接调用测试只允许解包/参数适配。
- [x] 7. 合并前路由（issue-scribe）：(a) pass 外删除面前沿口径 follow-up
  （D5：`scripts/node27_raw_retention.py` + `cli.py cleanup` 同单）→ #1407
  （scribe 核实修正：node-27 脚本已有 display watermark 锚，但系上界水位非活
  跃下界，缺口同形）；(b) node-22 live receipt rollout issue（D7，
  oracle-blocked，含实配反推核实项）→ #1406；(c) 双 run_id 解析器统一卫生债
  （D3）→ #1405（scribe 补充 B 类形状：runs/ 下非 run 目录含 10 位 token 被
  宽松解析纳入删除面；cohort run 依赖宽松解析回收，收严须显式纳入）。

## Round-1 fix tasks (Phase 5/6)

- [x] 8. T-2：`tests/test_retention.py` 双重 floor 测试补 `lookback_hours=170`
  用例（非网格整倍数），断言 `bound == datetime(2026,5,26,12,tzinfo=UTC) ==
  captured["start_time"]`——钉住外层 floor（删除方向 4h 未保护带）。
- [x] 9. T-4(c)：稳态 parity 测试给 in-window cycle 种 `run=True`，使 run 车
  道判定顺序翻转经既有 `skipped` 相等 + `protected_count == 0` 断言变红；顺
  带 T-1：退化测试补 `assert (bound, source) == (None, None)`。
- [x] 10. I-1/I-2 文档行：`docs/runbooks/two-node-deployment-overview.md`
  §8.2 retention 判据补前沿豁免一句（指向 `retention.skipped` 的
  `pipeline_frontier_exempt` 与 `retention.frontier.protected_count`）；
  `infra/env/compute.example` 两处更正——豁免明细在 `retention.skipped`（
  frontier 块只有 bound/source/count）、不变量实例化按 12h 网格
  `2×12=24 → 198 ≤ 336`。

## Required evidence (maps every selected pack)

- 追赶期 C 老于 cutoff + bound ≤ C → 豁免 + `pipeline_frontier_exempt` +
  frontier 块 + 两 reason 可区分。[File IO, Schema]（AC1，issue 主锚点）
- 终结 C → 照删、freed_bytes 不变。[Legacy]（AC2）
- 稳态逐 key parity（合规配置钉定）+ 判别性对照。[Legacy]（AC3）
- 边界配置（lookback=336）→ 豁免发生、fail-safe 方向。[Legacy, Error
  handling]
- 失败 run 工作区豁免、SHUD 日志留存。[File IO, Error handling]（AC4）
- helper 三源语义（终态排除法 + 未知 reason 保护侧 + 窗口地板 + None 退化 +
  字符串解析 fail-safe）。[Error handling, File IO]
- pass 级端到端：`run_once` 后 receipt frontier 块非 null 且豁免生效（线程化
  真接上）。[Error handling, Schema]（round-2 P1-2）
- 窗口地板双重 floor 公式钉 + 取整带 plan 用例。[File IO, Legacy]（round-2
  P1-1）
- 豁免项无 size_bytes、零 rglob。[Resource limits, Schema]
- 压缩态 frontier 块存活、既有压缩 pin 不弱化。[Schema]
- enabled/dry_run/forced-dry-run 门控不变（既有测试零回归覆盖）。[Legacy]
- compute.example 注释按 D6 措辞更新且不删数值示例。[Documentation]
- Commands: `uv run pytest -q tests/test_retention.py`、`uv run pytest -q
  tests/test_production_scheduler.py -k retention`、`uv run ruff check .`、
  `openspec validate retention-pipeline-frontier-exemption --strict
  --no-interactive`。

## Non-goals

- `STATE_CHECKPOINTS_MISSING` 数值/配置根因（现场需本 change 保全后才可查，另
  行开单）；#1203（URI 缺失误判）不重复申领；
  `NHMS_SCHEDULER_REPAIR_MISSING_FORCING` cycle 作用域 operability 另单；
  node-27 timeseries/DB retention（#1227/#1228 等，另一子系统——注意 #855/#856
  gated-enforce 指 timeseries 系统，与本模块 enabled/dry_run 结构相似但非同一
  语义源，fixture 不得混引）；`scripts/node27_raw_retention.py` 落地（D5 路
  由）；双 run_id 解析器统一（D3 路由，含"第二个 10 位 token 取更早时间戳"的
  既有低概率反向风险）；per-run journal 恢复态查询 API（D2 裁定不建）。
