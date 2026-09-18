# 任务

实现分三个互不重叠的写集（并行 worktree，见 `.workplans/pr-b10/parallel-worktree-manifest.md`）。
所有 red 腿都要对改前源码实跑并贴输出；node-27 相关项在 PR head 的隔离 worktree 上跑，绝不连生产 `nhms` 做写。

## A. 瓦片身份与预算（#2156 · #2165 · #2166）— `apps/api/routes/hydro_display.py`、`services/tiles/mvt.py`

- [x] A-1 `_river_network_source_version` 投影并摘要 `segment_count, checksum, geometry_generation`（D1）
- [x] A-2 `_run_row` 与 `display_ready_run` 同加 `LEFT JOIN core.river_network_version rnv` 投影 `geometry_generation`；
      `_run_source_version` 的 `revision_basis` 加该字段
- [x] A-3 单测（值级）：同一 `basin_version_id` 在 generation 0→1、`segment_count` 变、`checksum` 变 三种情况下
      `_river_network_source_version` 各返回不同串；未变时逐字节相等
- [x] A-4 单测：同一 run 在 generation 0→1 后 `_run_source_version` 变；`_run_row` 与 `display_ready_run` 都返回该字段；
      对同一 run 两者喂出的 `_run_source_version` 相等（目录/瓦片一致）
- [x] A-5 单测：两条路由的 `cache_key(TileInput(...))` 随之改变（复用 `services/tiles/mvt.py::cache_key` 值级断言）
- [x] A-6 凡会走到 `_river_network_source_version` / `_run_row` / `display_ready_run` 真 SQL 的 sqlite 夹具：有该表且含
      `geometry_generation`（及 `segment_count`/`checksum`），全绿
- [x] A-6b 有意重钉（D4 契约变化，不是放宽；每处在 docstring 写明来由）：
      `tests/test_hydro_display_mvt_scaling.py` 的 `NATIONAL_DISCHARGE_QUERY_VERSION == "fair-network-budget-v5"` 钉 → v6（docstring 同改）；
      `_PRE_2153_FULL_COVERAGE_TILE_BINDS["feature_limit"]` 10000 → 20000；`_PRE_2153_FULL_COVERAGE_CACHE_KEY` 新值**不得**由被测模块
      重算：在基线树 258b06ecf 上**只把** `NATIONAL_DISCHARGE_QUERY_VERSION` 字面量改成 v6、跑同一请求捕获，并实测改回 v5 复现旧值
      `f408b0df…`（证明只有版本号在动），两次输出贴 PR；
      `tests/test_river_ts_read_path_surrogate_keys_integration.py` 的 `monkeypatch.setattr(hydro_display, "MVT_MAX_FEATURES", 2)`
      改为打在 `feature_limit` 实际读取的常量所在模块，`bind["feature_limit"] == 2` 与 413 断言原样保留
- [x] A-7 `feature_limit(layer)` + `NATIONAL_DISCHARGE_FEATURE_LIMIT = 20_000`；bind / 413 判据与 details /
      `MVT_TILE_BUDGET_TRUNCATED.max_features` 同源；`NATIONAL_DISCHARGE_QUERY_VERSION = "fair-network-budget-v6"`
- [x] A-8 单测：`feature_limit("hydro-national") == 20_000`，其余四层与 `None` == `MVT_MAX_FEATURES`；
      `_postgis_tile_params(..., layer=L)["feature_limit"]` 对五层逐一断言；hydro-national 桩行
      `feature_count=15000, intersecting=15000` → 200 且零 TRUNCATED；`20000/21000` → TRUNCATED 的 `max_features=20000`
- [x] A-9 `MVT_TILE_FEATURE_OVERFLOW_BLANKED` WARNING（D5），上限值取自 bind
- [x] A-10 单测（桩行 `"tile": None`，真实 overflow 形状）：`feature_coordinate_overflow_count=1` → 一条 BLANKED、
      零条 TRUNCATED、返回 `b""`；`coordinate_dimension_overflow_count=1` 同；两计数皆 0 → 零条 BLANKED；
      每条 record 的 `extra` 含 D5 全部字段且上限值等于 bind；既有 case d/e 不改期望且绿
- [x] A-11 `test_every_column_the_tile_route_reads_is_projected_by_every_layer` 继续绿（新读列已五层投影）；
      `postgis_tile_sql(layer)` 五层文本逐字节不变（A 组不改 SQL）
- [x] A-12 node-27 throwaway-DB 集成（扩 `tests/test_mvt_national_identity_probe_integration.py` 的 generation 形态）：
      种一个网络 + 一个 display-ready run；回填前后各取同一 z/x/y 的 `river-network` 与 run 级 `hydro` 瓦片，
      断言 `X-Tile-Cache-Key` 与字节都变；空转回填后两者都不变

## B. 几何写面与锁序（#2154 · #2157）— `workers/model_registry/*`、`tests/test_river_segment_write_surface_scan.py`

- [x] B-1 `_lock_river_network_version` helper（D2）；`_backfill_output_segment_geometry` 入口第一条语句调用；
      `seed_qhh_output_segments` 与 bootstrap 主路径在 `_seed_output_segment_rows` 之前调用；两处 docstring 写锁序不变量与其边界
- [x] B-2 node-27 throwaway-DB 两会话交错测试：A `UPDATE core.river_network_version`（持锁）→ B 调
      `_backfill_output_segment_geometry` → A `UPDATE core.river_segment`（B 的目标行）→ A commit。
      判别式：修复后 A 的 segment UPDATE 在 B 仍等待期间**返回**（B 未持任何 segment 行锁），A 提交后 B 成功、两侧均提交；
      red 腿（改前源码）同一交错产出 `deadlock detected`（任一会话报出均算，取决于 `deadlock_timeout`），输出贴 PR。
      仅凭 `wait_event_type = 'Lock'` 不能区分两个构建（改前 B 也在 rnv 上等），不作判别式
- [x] B-2b 两会话，逐步：(1) 前置：该网络已提交可填行（NULL geom 或缺 `Type`）；(2) A 显式调 `_lock_river_network_version`
      （或等价 `SELECT … FOR NO KEY UPDATE`）不提交；(3) B 启动 `seed_qhh_output_segments` 同网络——red 构建（「只给 helper 加锁、
      seed 不加」的临时补丁，不入库）上 B 的 upsert 完成、随后在父行上等待，以 `pg_stat_activity`（B 在等）+ `pg_locks`（B 持
      `river_segment` 行锁）确认后再进行下一步；green 构建上 B 在 upsert **之前**等待；(4) A 调
      `_backfill_output_segment_geometry(only_missing=True)`。期望：red → `deadlock detected`（任一会话）；green → A 完成提交、
      随后 B 完成提交。两腿输出贴 PR
- [x] B-3 空转断言：几何完备网络 `only_missing=True` → 返回 0、恰一条父行 `FOR NO KEY UPDATE` 且它是第一条语句、零条
      `geometry_generation` 更新（fake cursor 记录语句序列），真实 DB 上 generation 不变；`only_missing=False`（seed 路径）→ 仍回填并 +1；
      任何路径下父行锁语句都先于第一条 segment 写
- [x] B-3b B 组有意重钉（D2 契约变化，不是放宽；docstring 写明）：`tests/test_hhe_mvt_binding.py` 中断言
      `cursor.statements[0]` 为候选 SELECT 的改为 `statements[1]`，并新增 `statements[0]` 为父行 `FOR NO KEY UPDATE` 的断言；
      以子串 `"UPDATE"` 过滤「无写」的断言改为按写语句（`UPDATE core.` / `SET`）过滤，保留「无写」意图，并断言唯一的非 SELECT
      语句是那条锁。实现者还须 grep 全部 fake-cursor 语句序列断言（`statements[`、`"UPDATE" in`）找出其余受影响处并同样处理、逐条报告
- [x] B-4 Type 完备性 helper（D3-1）接入 `seed_qhh_output_segments` 与 bootstrap 主路径；code
      `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE`
- [x] B-5 真实 DB 红-绿：先回填使 output 行带 `Type`，再让源 reach 的几何退化（`ST_Length = 0`）使尾随 backfill 返回 0，
      跑 `seed_qhh_output_segments`：改前 → 提交成功、`stream_type` 被擦成 NULL、generation 未变（不变量被违反的实证）；
      改后 → 抛该 code、事务回滚、`stream_type` 与 generation 均为改前值
- [x] B-6 写面扫描：generation 写的全形状计数（D3-2）、`db/` 纳入（D3-3）、两处 rnv 写方不含该列的显式钉、非空性自检含 `db`；
      同一改动把 `"db/**"` 加进 `scripts/select_ci_tests.py::RIVER_SEGMENT_WRITE_SURFACE_ROOTS` 并让 `db/**/*.sql` 也路由到本扫描，
      `tests/test_select_ci_tests.py` 的对应 meta-guard 绿（必要时补 `.sql` 路由用例）
- [x] B-7 扫描的合成用例（对分类函数，不改生产代码）：`SET geometry_generation = 0`、`SET geometry_generation=geometry_generation+2`、
      `UPDATE core.river_network_version SET segment_count = 1, geometry_generation = 5`、INSERT 列表含该列、
      一段 `.sql` 文本 `UPDATE core.river_segment SET geom = …;` 各自被判为违规；`SELECT rnv.geometry_generation FROM …` 不被判
- [x] B-8 D3-4 / D3-5 裁决只在规格落地（本 change 的 spec delta），代码不改

## C. 回收 runner（#2160）— `scripts/node27_mvt_cache_retention{.py,_once.sh}`、`tests/test_node27_mvt_cache_retention.py`

- [x] C-1 `fstat` 归类；monkeypatch `os.fstat` 抛 `OSError(errno.ESTALE)` → summary JSON 落盘、`counts.failed == 1`、
      `failed[0].error_type == "OSError"`、rc 1
- [x] C-2 `<hh>` 在父列举后消失（monkeypatch scandir 抛 `FileNotFoundError` / `NotADirectoryError`）→ `failed[] == []`、rc 0；
      `.locks/<hh>` 同理
- [x] C-3 `<hh>` `EACCES` 与 cache root 不可读两条既有用例不变且绿；cache root 自身 ENOENT 仍进 failed
- [x] C-4 `<root>/.locks` 自身 `chmod 0o000`（lstat 通过、scandir `EACCES`）→ 一条 `enumeration_unavailable`、rc 1，瓦片车道仍删除
- [x] C-5 post-flock `lstat` 抛非 ENOENT `OSError` → `failed`；`unlink` ENOENT → `already_gone`，各一测
- [x] C-6 wrapper `SUMMARY_PATH` / `LOG_FILE` / `LOCK_PATH` 相对值 → blocked（`SUMMARY_PATH_NOT_ABSOLUTE` / `LOG_FILE_NOT_ABSOLUTE` / `LOCK_PATH_NOT_ABSOLUTE`），
      runner 不执行，子进程测试后仓根 `git status --porcelain` 为空；`bash -n` 通过
- [x] C-7 既有断言无一放宽

## D. 规格、receipt 与收尾（orchestrator）

- [x] D-1 spec delta：`mvt-tile-contract`（I-geom 扩展 + D3-4/D3-5 边界）、`postgis-tile-clipping-cache`（按层预算 + BLANKED）、
      `mvt-tile-cache-lifecycle`（D6 carve-out、fstat、wrapper 卫兵）；`openspec validate --strict` 通过
- [ ] D-2 集成后对全部新增/改动测试文件核对选择器路由（`scripts/select_ci_tests.py` 由 B 组单一 owner；A/C 组若需新规则，集成后串行委派）
- [x] D-3 node-27 receipt `docs/runbooks/receipts/2026-09-18-batch-10-mvt-followups-node27.md`：
      (a) `geometry_generation` 列存在；(b) 生产 bind、只读角色 `nhms_display_ro`，`hydro-national q_down` 最新 valid_time
      的 z0–z5 全部中国瓦片：零 `MVT_TILE_BUDGET_TRUNCATED(layer_id=discharge)`、零 413，逐 zoom 记 feature 与 coordinate 的
      选中/相交、最大字节、冷生成耗时（z0 基线 29 s）与一次热命中耗时，
      z0–z3 新旧字节 + md5；(c) 同一 SQL 的最新完整 gfs cycle 路由 z0–z3 同样零截断；
      (d) `river-network-national` z3–z7 516 张仍零 TRUNCATED、零 BLANKED；(e) 强制低 `feature_coordinate_limit`
      在 3/6/3 上 BLANKED 响一次且瓦片零要素；维度超限一支的 oracle 是桩行单测（写明分工）；
      (f) 缓存 key 一次性轮转说明（river-network、run 级 hydro、全国 discharge）

## Evidence Floor

| issue | 验收 | 证据 |
|---|---|---|
| #2156 | digest 含 generation、值级变化、cache_key 变、node-27 瓦片字节变、sqlite 夹具绿、部署依赖说明 | A-1..A-6、A-12、D-3(a)(f) |
| #2154 | 1 fail-closed + 红绿；2 写钉全形状；3 db/ 纳入；4/5 裁决入规格 | B-4..B-8、D-1 |
| #2157 | 父行锁先于 segment 写；两会话红绿实跑；空转只取父行锁、不 bump（AC3 另一支）；seed 行为不变；锁序写入 docstring | B-1..B-3、B-2b、B-3b |
| #2166 | overflow 事件带全字段；桩行 tile=None；TRUNCATED 判据与 case d/e 不变；SQL 零变更；node-27 强制低限实证 | A-9..A-11、D-3(d)(e) |
| #2165 | 裁定落 design D4；node-27 z0–z5 零截断零 413；QUERY_VERSION bump + 新旧字节/md5；其余层仍 MVT_MAX_FEATURES 单测 | A-7、A-8、D-3(b)(c)(d) |
| #2160 | 六条行为验收 + wrapper 卫兵 + 规格一致 + 既有测试不放宽；issue 的 `openspec validate mvt-tile-cache-lifecycle-retention` 因该 change 已归档（PR #2163）不可执行，由本 change 的 validate 取代（偏离记录） | C-1..C-7、D-1 |

Verification（本地）：`uv run ruff check .` · `bash -n scripts/node27_mvt_cache_retention_once.sh` ·
`openspec validate mvt-cache-identity-and-budget-followups --strict --no-interactive` · 定向 pytest（非 DB 部分）。
Verification（node-27，`mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`）：上列全部触及测试文件的 `uv run pytest -q`
+ `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<owner role, throwaway>` 下的集成文件 + D-3 receipt。
