# Design: national-identity-probe-lateral-pushdown

## 风险三元组

- 级别：**expanded**——生产展示面计划形态改动（连续两单的最重发现都在这一类：
  #1442 的 segment 谓词清零、#1443 round-3 的 national 腿退化都是 EXPLAIN 门
  拦下的 P1），且触用户裁定的特批面扩宽；无迁移、无写路径、无 API 契约变更。
- must-preserve：(1) `source_identity_count` 语义逐字节不变（0/1 两分支，
  含"coverage 窗覆盖但该时次无行"的 0 分支——本探针必须触 fact 表的根本原因）；
  (2) 有数据 pin 上 tile 字节相同；(3) 未压缩时次毫秒级不退化；(4) 文本 fact
  join 禁令在特批探针体外不破（基线 spec:195-196）；(5) 计划判据以
  BUFFERS/batches/Rows Removed 为主证、wall time 为辅（#1442 教训：争用期
  wall time 会说谎），验收含"无覆盖压缩时次空瓦片 <1s"（issue 补充实测的
  0.17s 旧文本形态是可行性证明）；(6) `source_identity_stats` CTE 名不变
  （`test_river_ts_read_path_surrogate_keys.py:100-101` 与
  `_integration.py:176` 两个 slicer 以其为锚）。
- seams under test：`postgis_tile_sql("hydro-national")` 渲染函数（探针 CTE
  形状）；424 分支（`apps/api/routes/hydro_display.py:501-550` 消费
  `source_identity_count`）；形状 oracle 断言函数。

## D1: 方向裁定——(a) 逐身份 LATERAL 下推；(b) coverage 侧答被验收标准否决

勘察（PR #1655 后续勘察，read-only @c3027446）确立三个事实：

1. 现探针内层 `lr` 子查询与共享 `latest_runs` CTE（mvt.py:630-647）**同一
   门控形状**（同 `run_display_coverage` 门控：`segment_count>0` + valid_time
   窗 + forecast/active/status 过滤；仅 SELECT 列数不同——内层 2 列、共享 CTE
   4 列）。故 (b)（去掉 fact join、只答 `EXISTS(lr)`）是单谓词严格弱化：只
   可能 0→1 翻转（假阳性），不可能 1→0。
2. 假阳性发生面：`river_valid_time_start/end` 是**完整时刻**上的 MIN/MAX
   （display_coverage.py:439-456），窗内不保证逐时刻连续——内部空洞时次
   现行为 424，(b) 会翻成 200 + 空瓦片。验收标准原文："存在性语义逐字节
   不变：同一 (variable, valid_time) 下 `source_identity_count` 与现实现
   取值一致（含'无 display-ready run'的 0 分支），national tile 输出 MVT
   解码 feature 集合等价"——(b) 违反，否决。
3. 424 行为全仓零测试覆盖（`grep MVT_LIVE_POSTGIS_UNAVAILABLE tests/` 无
   命中）——(b) 类回归今日无门可拦。本单顺带补上该 oracle（D4）。

(c) 缓存：首个未命中仍付 30s、压缩边界变动即重现，issue 自身不倾向，否决。

## D2: 探针重塑形态（内联 discovery + 逐身份 LATERAL；审查修订版）

**作用域裁定（fixture 审查 P1-2）**：共享 `latest_runs` CTE 嵌在 `source_rows`
子查询内部的 WITH（mvt.py:597 内层），对外层兄弟 CTE `source_identity_stats`
词法不可见——"直接引用共享 CTE"不可行（会 `relation does not exist`，且本地
全部形状钉是字符串断言拦不住）。定案：**探针保留自带内联 discovery 子查询**
（与 `latest_runs` 同一 display-coverage 门控形状），SELECT 列从 2 列扩到
4 列（补 `h.run_id`、`mi.river_network_version_id` 文本，为 LATERAL 辅助
供值）；`tests/test_river_ts_read_path_surrogate_keys.py:438` 钉死的旧 2 列
字符串随 task 1.3 重钉。不做 `latest_runs` 上提（避免动 :374-378 的
source_rows 切片钉与 MATERIALIZED 语义面，消重收益不值该半径）。

```sql
source_identity_stats AS (
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM (
            SELECT DISTINCT ON (mi.river_network_version_id)
                   h.run_key, rnv.river_network_version_key,
                   h.run_id, mi.river_network_version_id
            FROM hydro.hydro_run h
            JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
            JOIN core.river_network_version rnv
              ON rnv.river_network_version_id = mi.river_network_version_id
            JOIN hydro.run_display_coverage rdc
              ON rdc.run_id = h.run_id AND rdc.segment_count > 0
             AND rdc.river_valid_time_start <= :valid_time
             AND rdc.river_valid_time_end >= :valid_time
            WHERE h.status IN ('succeeded','parsed','published')
              AND mi.river_network_version_id IS NOT NULL AND mi.active_flag
            ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC
        ) lr
        CROSS JOIN LATERAL (
            SELECT 1
            FROM hydro.river_timeseries ts
            WHERE ts.run_key = lr.run_key
              AND ts.river_network_version_key = lr.river_network_version_key
              -- transitional compressed-chunk pushdown aids, remove with #1342
              AND ts.run_id = lr.run_id
              AND ts.river_network_version_id = lr.river_network_version_id
              AND ts.variable = :variable
              AND ts.variable_e = (
                      SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e
                      WHERE e::text = :variable
                  )
              AND ts.valid_time = :valid_time
            LIMIT 1
        ) hit
        LIMIT 1
    ) THEN 1 ELSE 0 END AS source_identity_count
)
```

- **枚举匹配用 enum_range 形态**（fixture 审查 P1-1）：与数据腿 mvt.py:752-755
  一字不差——`(:variable)::hydro.river_variable` cast 被
  `tests/test_river_ts_read_path_surrogate_keys.py:198-231` 明令禁止（词表外
  字面量会 22P02，违反基线 spec 的 degrade-to-empty Scenario）。
- LATERAL 体内 `lr.*` 为 per-loop 常量 → `run_id`/`river_network_version_id`
  文本等值获得压缩 segmentby 剪枝（第 1、2 列）与未压缩文本索引路径，
  `(variable, valid_time)` orderby 批级 min/max 进一步消批——机制与 #1341
  数据腿完全同构（数据腿实测 0.013-0.086ms/loop）。
- **无 segment 相关**：本探针不需要逐 segment（EXISTS 一行即真），lr 基数
  = 活跃 network 数（~19）。**短路语义**：EXISTS 命中即停（有数据时次探最多
  一两个身份）；未命中侧（无数据/内部空洞）会把全部候选身份探完——每身份
  一次参数化探针，成本 ~19 × 亚毫秒，可接受且正是语义等价所必需。
- 无覆盖时次：discovery 子查询空集 → EXISTS 假、零 fact 触达——38s 空瓦片
  回归即刻消失（该腿加速不依赖任何下推）。
- 内部空洞时次：LATERAL 实际探 fact 表 → 0 分支保持（语义等价的关键）。

## D3: 特批扩宽（用户裁定面，随 PR 偏离记录呈报）

基线 spec:196 round-3 特批限"两个 hydro-national LATERAL 探针体"。本单扩至
第三个（身份存在探针体），受批列 = `run_id` + `river_network_version_id`
（本体无 segment 相关，**不含** `river_segment_id`——比数据腿更窄），各与键
对应物同合取式、`remove with #1342` 标记。协议沿 #1443 round-3 原例：spec
delta 修改 + PR 偏离记录呈报用户复核。形状 oracle 相应重钉：承重钉
`test_national_identity_probe_uses_the_same_key_shape_as_the_data_legs`
（:432-441）改断言新 LATERAL 形态与 `{run_id, river_network_version_id,
variable}` 辅助集（勘察确认现钉只允许 `{variable}`）。

## D4: 424 语义 oracle（真实 DB integration）

新文件 `tests/test_mvt_national_identity_probe_integration.py`（integration
marker，node-27 真实 DB 跑）：

1. 无 display-ready run（无 coverage 行）→ tile 端点 424
   `MVT_LIVE_POSTGIS_UNAVAILABLE`；
2. **内部空洞**：造一个 coverage 窗覆盖 `:valid_time` 但该时次无 fact 行的
   run（写窗端点两时刻的行、跳中间时刻、refresh coverage）→ 424——此用例
   即 (a)/(b) 的判别器，(b) 类回归在此转红；
3. 有数据 → 200 且 MVT 非空。

设计注记（fixture 审查 P2-3/P3-2 增补）：

- **防真空**：live 未启用与探针为 0 返回**同码同状态** 424
  `MVT_LIVE_POSTGIS_UNAVAILABLE`（hydro_display.py:490-498 vs :543-549，仅
  details 不同），而 `tests/integration_helpers.py` 的 `set_integration_env`
  不设 `NHMS_ENABLE_LIVE_POSTGIS_MVT`。测试 MUST 显式
  `monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")` 并断言 424 的
  `details` 含 `z/x/y` 而非 `required_env`——否则用例 1/2 因 live 未启用而
  假绿，(a)/(b) 判别器变哑。
- **造数前提**：run 必须 `run_type='forecast'`；`met.forcing_version` 行必须
  在场（display_start/end 由 GREATEST/LEAST 与 fv 窗共同决定，缺行则窗 NULL
  全滤）；`expected_segment_count` 取自 `mi.resource_profile` 或
  `rnv.segment_count`，端点两时刻必须写满 segment_count =
  expected_segment_count（否则不计入窗）——按 display_coverage.py:80-118 与
  :439-456 口径造数。
- **缓存隔离**：三用例使用不同 `valid_time`（`_cached_or_generated_mvt_
  response` 有瓦片缓存，复用时次会串味）。
- 端点级先例：`tests/test_real_database_integration.py:212` 的
  `TestClient(app)` + `set_integration_env` 模式；national 路由不做 coverage
  预校验，内部空洞时次能走到探针。

## D5: 测试策略

- 形状层（sqlite-free，渲染断言）：探针 LATERAL 形态、辅助集 ⊆ D3 允许集且
  键配对、CTE 名不变、`EXISTS` 包装保留（`test_sql_shape_helpers.py:572` 的
  剥离器自测前提）；`test_sql_shape_helpers.py:902-917` 的 fact 谓词存活断言
  经勘察确认对数据腿锚定、本改动下自然存活。
- 语义层：D4 三分支 integration oracle。
- E4 node-27 硬门（BUFFERS 主证；fixture 审查 P2-1/2/4 增补）：
  - **指名 pin**（issue 补充实测坐标，落 chunk 55——先回填后压缩，字节比对
    非空有意义）：压缩有覆盖 = z4 12/6 @ 2026-08-12T12Z；压缩无覆盖 =
    z9 407/200 @ 同时次；未压缩 = 当批时次任一 z4。若 retention 已推进致
    pin 失效，按同判据另选并在 receipt 记录选择依据。
  - **E4 preflight**：逐 pin 落 receipt——所触 chunk `is_compressed`、键
    NULL 计数（chunk 32/51 键全 NULL 不回填，误选会退化成"空对空"取证）、
    覆盖该时次的 `run_display_coverage` 行在场性，以及所触 chunk 的
    `reltuples`/`last_analyze`（压缩 chunk 统计清零陷阱，#1378/#1442 家族
    ——计划形态结论必须能对统计态归因）。
  - **before/after 必须同一安静库会话内成对重采**（before 用当前 master
    shipped SQL，不引用 issue 里 pre-#1341/争用期数字——#1442 的"203/259ms
    误标"教训）；warm 二采取第二采；不走 issue 提到的仓外 shape_explain.py
    脚手架。
  - 判据：压缩有覆盖 → 亚秒 + 解压 batches 与候选身份数同量级 + tile 字节
    相同；压缩无覆盖 → <1s 空响应、零 fact 触达；未压缩 → 毫秒不退化 +
    字节相同；z4 端到端压缩时次进秒级（#1341 AC-1 口径）。

## D6: select_ci_tests 观察（报告不改）

mvt.py 规则已含承重钉套件；`test_sql_shape_helpers.py:902-917` 不被 mvt.py
diff 选中属 #1597 已立单的家族缺口（4 个非门控 importer suite），本单不修。
