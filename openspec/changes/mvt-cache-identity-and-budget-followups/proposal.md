# MVT 缓存身份、预算与回收 runner 的跟进收口（第 10 批）

## Why

用户点名 8 个 issue 合为一个 PR。其中 **#2087**（PR #2455）与 **#2153**（PR #2344）在本批开工前已被合并的 PR
关闭，不在本 change 内；剩余 6 个都来自 #2030 / #2031 / #2032 三个 PR 的复审或 live receipt，分三组：

- **几何身份组**（同一条不变量：「瓦片 SQL 读到的河网几何被原地重写 ⇒ 缓存身份必转」）
  - **#2156** 单流域 `river-network` 与 run 级 `hydro` 两条瓦片的 digest 看不见 `geometry_generation`，
    一次原地几何回填后这两层的缓存永久发旧字节（`river-network` 零自愈）。
  - **#2154** `geometry_generation` 的执法面 5 处缺口：qhh upsert 擦 `Type` 后若尾随 backfill 返回 0 就以未轮转
    状态落库且不 fail-closed；bump 正则只认 `+ 1`；扫描作用域不含 `db/`；`only_missing=False` 过度轮转；
    行级 INSERT/DELETE 不动计数器。
  - **#2157** backfill 先写 segment 后 bump 父行，与 `_import_basin` 的「先父后子」构成 ABBA，
    bootstrap/重导入窗口内并发会 `deadlock detected`。
- **瓦片预算组**（同一个绑定点 `_fetch_postgis_tile_bytes`）
  - **#2165** 全国流量层 feature 臂被 `MVT_MAX_FEATURES=10000` 截断。本批开工前在 node-27 生产库只读实测
    （`nhms_display_ro`，q_down，valid_time `2026-09-23T23:00:00Z`）：**不止 z3**，z0/z1/z2/z3 共 5 张中国瓦片
    相交数 13770/13770/13392/10991 全部超 10000；z4/z5 最大 7242/7035。
  - **#2166** 单要素坐标/维度超限让 `budget_gate` 空集，整张瓦片零要素 200、零日志、照常入缓存。
- **回收 runner 组**
  - **#2160** `node27_mvt_cache_retention.py` 的 `fstat` 未归类、`<hh>` 目录中途消失被判 failed、
    wrapper 不校验 `SUMMARY_PATH` / `LOG_FILE` 绝对性。

## What Changes

- 四个读 `core.river_segment` 的瓦片 digest 都投影所属河网的 `geometry_generation`（national 两个已具备）：
  本批补单流域 `river-network`（`_river_network_source_version`，并按 #2156 建议同时投影 `segment_count` /
  `checksum`，与 `national_river_network_source_version` 同基底）与 run 级 `hydro`
  （`_run_row` + 目录用的 `display_ready_run` + `_run_source_version`）。
- `_backfill_output_segment_geometry` 在任何 segment 写之前对父行取 `FOR NO KEY UPDATE`（锁序 rnv → segments）。
- qhh 两个 upsert 入口加 `Type` 完备性断言（fail-closed），扫描钉扩到「`geometry_generation` 的任何写」与 `db/`。
- 新增按层 feature 预算 `feature_limit(layer)`：`hydro-national` 取 `NATIONAL_DISCHARGE_FEATURE_LIMIT = 20_000`，
  其余四层仍取 `MVT_MAX_FEATURES`；`NATIONAL_DISCHARGE_QUERY_VERSION` 轮转到 `fair-network-budget-v6`。
- `_fetch_postgis_tile_bytes` 在任一 overflow 计数 > 0 时发 `MVT_TILE_FEATURE_OVERFLOW_BLANKED` WARNING。
- 回收 runner 三处健壮性收尾 + 规格同步。

不改：迁移/schema、`MVT_MAX_FEATURES` / `MVT_MAX_COORDINATES` 数值、`MVT_TILE_BUDGET_TRUNCATED` 判据、
413/424/500 语义、前端。

## Impact

- 代码：`apps/api/routes/hydro_display.py`、`services/tiles/mvt.py`、
  `workers/model_registry/{basins_registry_import,qhh_production_bootstrap}.py`、
  `scripts/node27_mvt_cache_retention{.py,_once.sh}` 及对应测试。
- 规格：`mvt-tile-contract`、`postgis-tile-clipping-cache`、`mvt-tile-cache-lifecycle`。
- 缓存：部署后单流域 `river-network`、run 级 `hydro`、全国 discharge 三类瓦片 cache key 各一次性轮转（一次冷 miss）。
- 部署：本批代码依赖 000057（`geometry_generation` 列，#2145 已关闭）；node-27 receipt 实测该列存在。
