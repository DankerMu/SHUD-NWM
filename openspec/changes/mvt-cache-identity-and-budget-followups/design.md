# Design — 第 10 批 MVT 跟进收口

## Risk Triage

```text
Issue type: bugfix (+ 一处产品裁定 #2165)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high —— 公开瓦片缓存身份、全国流量瓦片字节、生产写路径的锁序
Fixture level: expanded；repair intensity: high
Upstream suggested level: absent（手写 issue）
Why:
- 缓存身份（cache key）= 公开 display 身份，共享 helper `_run_source_version` 同时喂瓦片路由与 /api/v1/layers 目录
- 生产写路径的加锁顺序（concurrency / persisted shared state）
- 全国流量瓦片字节变化 + *_QUERY_VERSION 轮转（published artifact identity）
- 回收 runner 的删除路径与 wrapper 的文件输出路径（file IO / path）
Selected risk packs: Public API; Concurrency; File IO/path; Error handling/partial outputs; Resource limits;
  Legacy compatibility; Documentation; PostGIS/Timescale; Published artifacts/display identity
OpenSpec change: mvt-cache-identity-and-budget-followups (generated)
```

已关闭、不在本 change：#2087（PR #2455）、#2153（PR #2344）。issue 正文的 `file:line` 均钉在旧分支 head，
实现一律按符号定位。

## Decisions

### D1 — #2156：单流域 / run 级 digest 投影 `geometry_generation`

- `_river_network_source_version`：SELECT 扩为 `river_network_version_id, segment_count, checksum, geometry_generation`，
  digest 基底改为逐网络 `id|segment_count|checksum|geometry_generation`；返回串保留前缀 `river-network-set:` 与
  尾部 id 列表（目录可读性不变），key 一次性轮转。`segment_count`/`checksum` 采纳 issue「顺带」建议：同一条 SELECT、
  零额外成本，且让 #2154 第 5 项（行级 INSERT/DELETE 伴随 rnv 元数据刷新）对本层成立。
- `_run_row` 与 `services/tiles/mvt.py::display_ready_run`（目录的无 `run_id` 分支用它，与 `_run_row` 喂同一个
  `_run_source_version`）**两处同改**：`LEFT JOIN core.river_network_version rnv ON rnv.river_network_version_id =
  mi.river_network_version_id`，投影 `rnv.geometry_generation`。只改一处会让目录播报的 `source_version` 与瓦片路由
  算出的不一致——这是本批最重要的兄弟面。
- `_run_source_version` 的 `revision_basis` 加 `geometry_generation`（缺列 / NULL 时为 `None`），签名与调用点不变。
  所有 run 级 hydro key 一次性轮转。
- 不做：给 national discharge digest 加 `segment_count`/`checksum`（#2031 领地，本批不动）。
- 部署依赖：两条路由新增对 000057 的硬依赖（#2145 已关闭；receipt 实测列存在）。

### D2 — #2157：锁序 rnv → segments（两个原地写者都先锁父行）

原地改写**已有** `core.river_segment` 行的生产写者只有两个：backfill 的 `UPDATE`，和 qhh 的 output-segment upsert
（`ON CONFLICT DO UPDATE` 对已有行加行锁）。两者都改为先取父行锁，用同一个 helper
`basins_registry_import.py::_lock_river_network_version(cursor, rnv_id)`
（`SELECT 1 FROM core.river_network_version WHERE river_network_version_id = %s FOR NO KEY UPDATE`；前置条件「该行已存在」写进
 docstring——不 `fetchone()` 断言，但所有调用点都在 `_ensure_river_network` / 既有网络之后）：

- `_backfill_output_segment_geometry`：**函数入口第一条语句**取锁，先于候选 SELECT。选「入口取锁」而非「早退之后取锁」：
  锁下读候选与源 reach，避免拿锁前的快照在等待 A 提交后覆盖 A 刚写入的几何（fixture review 指出的陈旧快照窗口）。
  代价：几何完备网络上的空转 tick 也短暂持父行锁到事务末——issue AC3 允许的另一支「取锁但不 bump」，由 B-3 钉死
  「空转返回 0、不 bump」。空转 tick 只会在人工并发导入时排队，不会失败。
- `seed_qhh_output_segments`：在 `_seed_output_segment_rows` 之前取锁。**这是必须的**：只给 helper 加锁会让
  B1（autopipeline backfill，先父后子）与 B2（seed，upsert 先锁子行）从今天的「同序互等」变成新的 ABBA。
- bootstrap 主路径（`qhh_production_bootstrap.py` 的 `import_basin_into_registry_core` → upsert → backfill）：
  `_import_basin` 已先 `UPDATE core.river_network_version`，顺序本已正确；仍在 upsert 前显式调 helper（同事务重入无害），
  让「upsert 前持父行锁」对两个入口一致。
- `_import_basin` 内对 helper 的重入是同事务重复取锁，不自锁。`seed_qhh_output_segments`（`only_missing=False`）
  仍无条件回填并 bump（AC4）。
- 边界（写进 docstring 与规格）：不变量只覆盖「原地改写已有行」；行级 INSERT（`_ensure_river_segments`、
  `db/seeds/seed_demo.py` 的 `ON CONFLICT DO NOTHING`）与 `_delete_legacy_seg_rows` 不在内——前者不锁已有行，
  后者在 `_import_basin` 事务内、而该事务随后即 `UPDATE` 父行（与 backfill 同序：先子后父的只有它，但它只删
  `<model>_seg_*` 旧行，backfill 从不写这些行，锁集不相交）。
- 备选 advisory lock 被否：与 `_refresh_parent_version_materialization` 的普通 UPDATE 互不感知（issue 已论证）。

### D3 — #2154 五项裁决

1. **fail-closed**：新增一个共享断言 helper，两个 upsert 入口（`seed_qhh_output_segments`、bootstrap 主路径的
   `_assert_complete_qhh_output_segment_geometry` 同一处）都在尾随 backfill 之后、同一 cursor 上调用。谓词**复用 backfill 的
   候选/源谓词**，逐字如下（`t` = output 行，`s` = 源 reach）：
   `t.river_network_version_id = :rnv AND COALESCE(t.properties_json->>'shud_output_river','false') = 'true'
   AND (t.properties_json->>'shud_riv_index') ~ '^[0-9]+$' AND NOT t.properties_json ? 'Type'
   AND EXISTS (SELECT 1 FROM core.river_segment s WHERE s.river_network_version_id = t.river_network_version_id
   AND COALESCE(s.properties_json->>'shud_output_river','false') <> 'true' AND s.properties_json ? 'iRiv'
   AND (s.properties_json->>'iRiv') ~ '^[0-9]+$' AND s.properties_json->>'iRiv' = t.properties_json->>'shud_riv_index'
   AND s.geom IS NOT NULL AND s.properties_json->>'Type' IS NOT NULL)`。
   源侧 `Type` 判据取 `->> IS NOT NULL` 而非键存在 `?`：backfill 以 `properties_json->'Type'` 读出后仅在 `is not None` 时
   复制，键缺失与 JSON `null`（dbf 数值字段空值经 pyshp 读为 `None`）两者都算「无 `Type`」；用 `?` 会把 JSON `null` 源判成
   「可恢复却未恢复」而让 seed/bootstrap 永久回滚（round-1 cand-01）。
   **刻意不含** `ST_Length(s.geom) > 0`——这是与 backfill 的**唯一**差异，正是它让「源几何退化 → backfill 返回 0」被断言抓到（B-5 绿腿）。
   计数 > 0 即抛 `QhhProductionBootstrapError`（code `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE`，details 带 rnv 与计数），
   事务回滚。**行为变化（有意）**：源 reach 零长度但带 `Type` 的网络上，`seed_qhh_output_segments` 过去成功（报告
   `geometry_missing_count`），现在 fail-closed。该 code **不**加入 `_persist_inactive_on_scheduler_visibility_blocker` 的持久停用集合：回滚保留此前已提交的
   `Type`，缺 `stream_type` 只影响低 zoom 分级，不像缺几何那样让调度可见的模型失效。不选「upsert 自行 bump」：它只能让被擦掉的
   `stream_type` 以已轮转状态落库，数据本身仍是坏的。
2. **写钉换形状**：扫描对标识符 `geometry_generation` 的**任何写**计数——`SET … geometry_generation =`（任意右值）、
   INSERT 列表中出现、`UPDATE … SET` 多列中出现——全仓恰好一处（backfill 那一句）；000057 的 `ADD COLUMN` DDL
   按文件名显式白名单；只读 SELECT（`mvt.py` 两个 national digest、本批新增的读）不计。并显式钉
   `_ensure_river_network` 的 INSERT 与 `_refresh_parent_version_materialization` 的 UPDATE 不含该列。
3. **扫描 `db/`**：`PRODUCTION_DIRS` 加 `db`；`.py` 走 AST，`db/**/*.sql` 走文本（按 `;` 切语句后匹配，同样的
   `UPDATE core.river_segment` / upsert / generation 写规则）；非空性自检随之更新；**同一改动**把 `"db/**"` 加进
   `scripts/select_ci_tests.py::RIVER_SEGMENT_WRITE_SURFACE_ROOTS`（该文件的 meta-guard 解析 `PRODUCTION_DIRS` 并断言相等，
   且选择器需对 `.sql` 也路由到本扫描）。已知限：`;` 切分会把 `DO $$ … $$` 块切碎（000037），也看不见
   `ALTER COLUMN geom TYPE … USING` 形态的改写——今天都不影响任何计数，记为扫描边界。
4. **`only_missing=False` 过度轮转：接受并写进规格**。它与 #2157 AC4 冲突不可调和（seed 必须无条件 bump）；
   代价上界：每次运维触发的 bootstrap/seed 对每个涉及网络多一次全国 + 单流域冷 miss。
5. **行级 INSERT/DELETE 与 `backfill_output_segment_geometry=False` 导入：接受为不变量边界，写进规格**。
   理由：这些路径同时刷新 rnv 的 `segment_count`/`checksum`（`_refresh_parent_version_materialization`、
   `_delete_legacy_seg_rows`），而画「整网 segment」的两层（`river-network`、`river-network-national`）digest
   含这两列 → 必转；画「run 时序 × segment」的两层（`hydro`、`hydro-national`）由 run 身份驱动，行级路径不改
   已有 run 的时序行集合，按边界接受。

### D4 — #2165：按层 feature 预算（产品裁定）

裁定：**按层 feature limit**（issue 推荐项；备选 B 全局提限有 `services/tiles/mvt.py` 注释记录的否决先例，备选 A 改变制图口径）。
开工前实测（见 proposal）推翻了 issue「只有 z3」的前提：z0–z3 各一张（共 4 张）全被截，最大相交 13 770。取值
`NATIONAL_DISCHARGE_FEATURE_LIMIT = 20_000`：对 13 770 留约 45% 余量。字节：实测 160–202 B/feature
（z2 2/3/1 = 160，3/6/3 = 186，4/12/6 = 190，5/25/12 = 202）⇒ 20 000 约 3.2–4.0 MB，低于 `MVT_MAX_BYTES = 5_000_000`
（超限是**硬 413**，不是优雅截断，故不再往上取）；坐标：实测 2.0–2.3 coord/feature，20 000 ≈ 40–46k，仍低于该层
50 000 的集合坐标预算，即 feature 臂仍是先触顶的那一臂、截断仍走公平窗口 + 信号。「缺约 9% 段」从未被产品侧接受过（仓内无记录，仅有「返回可渲染 tile 而非 413」的兜底注释）。

- `services/tiles/mvt.py` 新增 `feature_limit(layer)`，仿 `collection_coordinate_limit`：`hydro-national` → 20 000，
  其余（含 `None`）→ `MVT_MAX_FEATURES`。
- `_postgis_tile_params` 的 `feature_limit` bind、`_fetch_postgis_tile_bytes` 的 413 判据与 details、
  `MVT_TILE_BUDGET_TRUNCATED` 的 `max_features` **同源**取 `feature_limit(layer)`。
- `_station_source_version` 的 `row_limit` 与内存编码 `_enforce_feature_budget` 只服务非 `hydro-national` 层，
  保持 `MVT_MAX_FEATURES`；由单测锁定「其余四层仍取 `MVT_MAX_FEATURES`」。
- `NATIONAL_DISCHARGE_QUERY_VERSION` → `fair-network-budget-v6`（缓存 key 不 hash SQL/bind，不轮转会继续发旧的截断字节）。
- 超过 20 000 时行为仍是公平窗口截断 + `MVT_TILE_BUDGET_TRUNCATED`（可观测，不 413）。

### D5 — #2166：overflow 清空瓦片发 WARNING，不改 HTTP / 缓存

在 424 判据之后、`return` 之前：`feature_coordinate_overflow_count > 0 or coordinate_dimension_overflow_count > 0`
→ 一条 `WARNING`，token `MVT_TILE_FEATURE_OVERFLOW_BLANKED`，与 `MVT_TILE_BUDGET_TRUNCATED` 同 logger、同
`extra=` 结构，字段 `layer_id z x y feature_coordinate_overflow_count feature_coordinate_count max_feature_coordinates
coordinate_dimension_overflow_count coordinate_dimension_count max_coordinate_dimensions`；两个上限值取自**本次 bind**
（`feature_coordinate_limit` / `max_coordinate_dimensions`），monkeypatch 后 receipt 报的就是实际生效值。
不选 5xx：错误路径永不缓存，每次请求重跑注定失败的 SQL，prewarm 逐瓦片记 failed。
附带裁决「清空瓦片是否跳过缓存写」：**不跳过**——与 TRUNCATED 一样按生成计数，改缓存语义超出本 issue。
SQL 零变更、无 `*_QUERY_VERSION` bump（四列已在共享 SELECT 投影）。

### D6 — #2160：回收 runner 收尾

按 issue 推荐三条：`fstat` 并入既有 try 的 `except OSError → ("failed", error)`；仅 `_lane_targets`（扫 `<root>/<hh>` 与 `.locks/<hh>`）
前置 `except (FileNotFoundError, NotADirectoryError): return [], None`；`_hex_directories` 只列 cache root 与 `.locks`，**刻意不加**（cache root / `.locks` 自身不豁免），`EACCES`/`ESTALE` 仍 failed；
wrapper 给 `SUMMARY_PATH` / `LOG_FILE` 各加 `LOG_ROOT` 同款 `case` 卫兵（`SUMMARY_PATH_NOT_ABSOLUTE` /
`LOG_FILE_NOT_ABSOLUTE`，blocked 在取锁与任何写之前）。同类兄弟 `LOCK_PATH`（env 模板里**有**文档化的变量，
`exec 9>` 在 `cd` 之前按调用方 cwd 解析）一并加 `LOCK_PATH_NOT_ABSOLUTE`。`BOOTSTRAP_LOG` 不纳入：它在 env 文件 source
之前读取、只能由已入库的 unit `Environment=` 设置，且是 `blocked()` 自身的 sink。原 change `mvt-tile-cache-lifecycle-retention` 已归档，
规格同步改在本 change 对 `openspec/specs/mvt-tile-cache-lifecycle/spec.md` 的 MODIFIED delta。

## Invariant Matrix

```text
Invariant Matrix (I-geom)
Governing invariant: 任何瓦片 SQL 读到的 core.river_segment.geom / stream_type 被原地重写时，读它的每个瓦片 digest
  （river-network、river-network-national、hydro、hydro-national）都必须变化，且目录播报的 source_version 与瓦片
  路由算出的一致；所有原地改写已有 core.river_segment 行的写者（backfill UPDATE、output-segment upsert）先持父
  core.river_network_version 行锁再写 segment。
Source-of-truth identity/contract: core.river_network_version.geometry_generation（+ 单流域层的 segment_count/checksum）
Surfaces:
- Producers: basins_registry_import.py::_backfill_output_segment_geometry（唯一 UPDATE + 唯一 bump + 入口父行锁）；
  qhh_production_bootstrap.py::_seed_output_segment_rows（upsert，擦 Type；两入口在其前取父行锁，其后尾随 backfill + 新断言）；
  行级写者 _ensure_river_segments / _delete_legacy_seg_rows / db/seeds/seed_demo.py（边界外，见 D2/D3-5）
- Validators/preflight: 新 Type 完备性断言（两入口）；_assert_complete_qhh_output_segment_geometry（geom）
- Storage/cache/query: hydro_display.py::_river_network_source_version / _run_row / _run_source_version；
  mvt.py::display_ready_run、national_* digests（不改）、cache_key、map.tile_cache、文件缓存
- Public routes/entrypoints: river-network 瓦片路由、run 级 hydro 瓦片路由、/api/v1/layers 目录（run_id 与无 run_id 两支）
- Frontend/downstream consumers: 前端只消费 source_version 字符串，不解析 → 不改
- Failure paths/rollback/stale state: 断言失败 → _transaction 回滚；死锁受害方回滚；旧缓存条目随 key 轮转自然失效
- Evidence/audit/readiness: tests/test_river_segment_write_surface_scan.py（写面钉，含 db/）；node-27 throwaway-DB 集成测试
Regression rows:
- backfill 更新 ≥1 行 → generation +1 → 单流域 river-network 与 run 级 hydro 的 source_version、cache_key、瓦片字节都变
- backfill 空转（only_missing=True、几何完备）→ 返回 0、只取父行锁、不 bump、四个 digest 逐字节不变
- upsert 擦 Type + 尾随 backfill 返回 0 → 抛 QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE、回滚、Type 保留、generation 不变
- 会话 A 持 rnv 行锁、会话 B 调 backfill → 修复后 B 串行等待、两侧成功提交；修复前同一交错 deadlock detected
- 目录无 run_id 分支（display_ready_run）与瓦片路由对同一 run → source_version 相等
- 生产代码 / db/ 中新增任意形式的 geometry_generation 写或第二个 UPDATE core.river_segment → 扫描测试红
- 不变的兄弟：_station_source_version（met.met_station，不读 river_segment）→ 不改；national 两个 digest 的 SQL/基底
  A 组不改（hydro-national digest 的前缀因 D4 的 v6 一次性轮转，A-12 / D-3 的「不变」比较都以 v6 为基线）
- 并发：autopipeline backfill（B1 形状，入口取父行锁）与 `seed_qhh_output_segments`（upsert 前取父行锁）同网络并发 →
  串行、两侧提交；「只给 helper 加锁」的构建上同一交错 → deadlock detected（B-2b 红腿）
```

```text
Invariant Matrix (I-budget)
Governing invariant: 任何让瓦片少画要素的路径（公平窗口截断、单要素 overflow 清空）都在生成时发出带层/坐标/计数/
  上限的 WARNING；上限在 bind、413 判据、信号三处同源。
Source-of-truth identity/contract: feature_limit(layer) / collection_coordinate_limit(layer)；共享 SELECT 的计数列
Surfaces:
- Producers: mvt.py::postgis_tile_sql 共享 SELECT（不改）
- Validators/preflight: none - 本批不加预检
- Storage/cache/query: NATIONAL_DISCHARGE_QUERY_VERSION（cache key 组成）
- Public routes/entrypoints: _fetch_postgis_tile_bytes（五层唯一绑定点）、_postgis_tile_params
- Frontend/downstream consumers: 全国流量瓦片字节变大（z0–z3）；前端不改
- Failure paths/rollback/stale state: 413/424/500 语义不变；旧截断瓦片随 v6 轮转失效
- Evidence/audit/readiness: node-27 receipt（生产 bind 零 TRUNCATED、强制低限 overflow 事件响）
Regression rows:
- hydro-national 相交 13 770 → bind 20 000 → 选中 = 相交，无 TRUNCATED
- hydro-national 相交 > 20 000 → 截断 + TRUNCATED(max_features=20000)
- 其余四层 → feature_limit == MVT_MAX_FEATURES，bind 不变
- 任一 overflow > 0（桩行 tile=None）→ 一条 OVERFLOW_BLANKED、零条 TRUNCATED、字节 b""、HTTP 200
- 两个 overflow 均 0 → 零条 OVERFLOW_BLANKED
```

Boundary-surface checklist：shared helper roots（`_run_source_version`、`_fetch_postgis_tile_bytes`、
`_backfill_output_segment_geometry`）；public entrypoints（三条瓦片路由 + 目录）；read surfaces（四个 digest）；
write surfaces（backfill、upsert、回收 runner 的 unlink）；stale-state（缓存 key 一次性轮转、旧 key 条目不清理）；
unchanged downstream consumers（national 两个 digest、`_station_source_version`、前端、`MVT_TILE_BUDGET_TRUNCATED` 判据）。

## Risk packs considered

- Public API / CLI / script entry: **selected** — 瓦片路由 source_version/cache key、目录、wrapper 退出码
- Config / project setup: not selected — 不改 env 模板/unit（wrapper 只加卫兵）
- File IO / path safety / overwrite: **selected** — wrapper sink 绝对性、runner 删除路径 fstat 归类
- Schema / columns / units / field names: not selected — 无迁移；只读已有列
- Auth / permissions / secrets: not selected — 不触及；receipt 只用只读角色，DSN 不落盘
- Concurrency / shared state / ordering: **selected** — D2 锁序 + 两会话交错实测
- Resource limits / large input / discovery: **selected** — D4 feature 预算与字节上限
- Legacy compatibility / examples: **selected** — legacy 无 source/cycle 的全国路由同 SQL；缓存 key 一次性轮转
- Error handling / rollback / partial outputs: **selected** — D3 fail-closed 回滚；runner 必落 summary
- Release / packaging / dependency compatibility: not selected — 无依赖变化
- Documentation / migration notes: **selected** — 规格三处、锁序 docstring、部署依赖 000057
- Domain: PostGIS / Timescale: **selected** — STORED `stream_type`、`FOR NO KEY UPDATE` 语义需真实 DB
- Domain: Published NHMS artifacts / display identity: **selected** — I-geom
- Domain: Operator alerting / observer-observed predicate parity: **selected** — BLANKED 的判据必须与 `budget_gate` 的两条
  overflow 谓词一致（A-10 桩行 + spec），TRUNCATED 的抑制条件不变
- Domain: Geospatial / CRS / basin geometry: not selected — 不改任何几何值或 CRS 变换；只改锁序与 `Type` 断言，
  B-5 构造零长度几何只是测试夹具
- 其余 domain packs（forcing、SHUD numerical、Slurm、providers、manifest/QC）: not selected — 不触及

## Seams under test

- 单流域/run digest：`_river_network_source_version(session, bv)`、`_run_source_version(run)`、`_run_row`、
  `display_ready_run` 的值级输出 + 瓦片路由 `X-Tile-Cache-Key`（sqlite 夹具 + node-27 throwaway DB）
- 写路径：`_backfill_output_segment_geometry(cursor, rnv, only_missing=…)`、`seed_qhh_output_segments`（真实 DB）
- 预算/信号：`_fetch_postgis_tile_bytes` 桩行（caplog）+ `_postgis_tile_params` / `feature_limit`
- 写面：`tests/test_river_segment_write_surface_scan.py` 的扫描 + 对分类函数的合成字面量用例
- runner：`run_retention` / `main` monkeypatch + wrapper 子进程

## Non-goals

- #2087 / #2153（已关闭）；#2032 缓存淘汰/TTL；已缓存旧条目的一次性 purge；national discharge digest 基底
- `MVT_MAX_FEATURES` / `MVT_MAX_COORDINATES` 数值；`MVT_TILE_BUDGET_TRUNCATED` 判据；前端呈现「被清空」
- `scripts/node27_raw_retention.py`（#2309 另行跟踪）；把 backfill 并入 cron flock 的运维串行化
- 触发器方案（#2154 备选）

## Review focus

1. `display_ready_run` 与 `_run_row` 是否投影同一 rnv 列，目录与瓦片的 source_version 对同一 run 是否相等
2. 父行锁是否先于**所有** segment 写；空转 tick 是否只取父行锁、零 bump
3. 写面扫描是否对「非 `+1` 的 generation 写」「db/*.sql 的 UPDATE」真能变红（合成用例），且不误伤只读 SELECT
4. `feature_limit(layer)` 是否在 bind / 413 / TRUNCATED 三处同源；其余四层不变
5. runner carve-out 是否只放行 ENOENT/ENOTDIR、cache root 自身不豁免
