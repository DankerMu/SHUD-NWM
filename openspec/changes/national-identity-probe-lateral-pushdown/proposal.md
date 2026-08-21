# Proposal: national-identity-probe-lateral-pushdown

## Why

Issue #1596（pre-existing @master，#1341/PR #1443 部署验证实测）：`hydro-national`
tile 的 `source_identity_stats` 存在性探针（`services/tiles/mvt.py:592-628`）
只绑定 `:variable`/`:valid_time`，run/network 身份经内层 `DISTINCT ON` 子查询
以 join 列到达——压缩 chunk 上无 segmentby 字面量可下推，整片解压 ~141M 行只为
答一个 EXISTS：压缩时次单次 23-37s；issue 补充实测证实 #1341 键切换后**无覆盖
压缩时次的空瓦片也从 0.17s 退化到 38s**（旧文本谓词能让 planner 提前判空，键
形态不能）。z4 压缩时次整块 tile 的 ~26-33s 残余全部归此 CTE（#1341 archive
design.md:98-124 明确划归本单）。mvt_prewarm 逐 tile 重执行该探针，成本按
prewarm 网格放大。

## What Changes

1. **探针重塑（方向 (a)，逐身份 LATERAL 下推）**：`source_identity_stats`
   （hydro-national 分支）改为对身份集合做 `CROSS JOIN LATERAL` 逐身份存在
   探针——身份成为 per-loop 常量，`run_id`/`river_network_version_id` 文本
   等值获得压缩 segmentby 剪枝与未压缩 run 作用域索引前缀——机制与 #1341
   数据腿同类但非同构（数据腿绑满列；本探针无 segment 相关项，代价分
   命中/未命中两侧，详 design D2，round-1 C1/round-3 D3 更正）。身份
   集合用**内联 4 列发现子查询**（键对 + 文本对；与 `latest_runs` 同门控
   形状）——共享 CTE 引用在词法上不可行：`latest_runs` 嵌在 `source_rows`
   的内层 WITH（mvt.py:597）里，对本 CTE 不可见（design D2）。
   `source_identity_stats` CTE 名不变（两个测试 slicer 锚定它）。
2. **spec delta**：round-3 特批从"两个 LATERAL 探针体"扩到第三个（身份存在
   探针体，受批列 = `run_id` + `river_network_version_id`，各与键对应物同
   合取式，`remove with #1342`）——用户裁定面的位置性扩宽，随 PR 偏离记录
   呈报（沿 #1443 round-3 同款协议）。
3. **424 语义 oracle（全仓空白，勘察实证）**：`MVT_LIVE_POSTGIS_UNAVAILABLE`
   路径零测试覆盖。新增真实 DB integration 测试钉三分支：无 display-ready
   run → 424；coverage 窗覆盖但该时次事实行缺失（内部空洞——正是本探针必须
   触 fact 表、coverage 侧答方案被否的判据）→ 424；有数据 → 200 非空 MVT。
4. **形状销钉重钉**：`tests/test_river_ts_read_path_surrogate_keys.py:432-441`
   （承重钉：探针键形 + 文本辅助集）按新形态重钉；slicer 锚
   （:100-101、`_integration.py:176`）靠 CTE 名不变维持。

## Non-Goals

- 方向 (b)（纯 `run_display_coverage` 侧答）：被验收标准（issue 原文）
  "存在性语义逐字节不变：同一 (variable, valid_time) 下
  `source_identity_count` 与现实现取值一致（含'无 display-ready run'的
  0 分支），national tile 输出 MVT 解码 feature 集合等价"否决——coverage 窗是完整时刻的 MIN/MAX 非逐时刻位图，
  内部空洞会把 424 翻成 200 空瓦片（详 design D1）。
- 方向 (c) 探针结果缓存（issue 自身不倾向）；prewarm 网格/频率调整。
- 其余三个 `source_identity_stats` 形状（default/`river-network-national`；
  勘察确认无 national 探针兄弟副本）。
- `run_display_coverage` 的 forecast-only/q_down-only/异步刷新语义（两方向
  同等继承的既有约束，报告不改）。
- #1342 后的下推面重估（该单已有本探针存续方式的显式说明义务）。

## Impact

- Affected specs: `river-identity-normalization`（MODIFIED：in-boundary
  readers requirement 的 round-3 特批段与两个 Scenario——两处"the two"扩为
  三个探针体）。
- Affected code: `services/tiles/mvt.py`（单 CTE 重塑）、
  `tests/test_river_ts_read_path_surrogate_keys.py`（承重钉重钉）、新增
  `tests/test_mvt_national_identity_probe_integration.py`（424 语义 oracle，
  integration marker）。
- 部署面：node-27 git pull + display API 重启；无迁移、无 timer 改动。
