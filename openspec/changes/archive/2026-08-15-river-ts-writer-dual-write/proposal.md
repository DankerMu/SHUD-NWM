# Proposal: river_timeseries 写路径代理键双写（issue #1340）

## Why

#1339（已合并，PR #1403）给 `hydro.river_timeseries` 加了 7 个 nullable 代理列
（4 键 + 3 ENUM），存量 459.9M 行由回填 runner 负责；但**新写入的行仍然只填
文本列**——每个 ingest cycle 都在制造新的 NULL 哨兵行，全量回填窗口永远追不上
写入。写路径双写（同一条 INSERT 同时写文本列与代理列）是止血步骤：新行代理列
零 NULL、`--final-sweep` 收敛条件可达、任何时刻回滚到旧代码不丢数据（文本列
仍是权威）。Epic #1336 的 M2 写入侧，必须先于读切换与旧列下线。

## What Changes

- **`workers/output_parser/parser.py`**（生产唯一写者，000050 头注释 +
  探索证据双确认）：`PsycopgOutputParserRepository.upsert_river_timeseries`
  的 INSERT 列表追加 7 列；4 个代理键随**既有两条 load 查询**取回（零额外
  往返，`HydroRunContext`/`RiverSegmentOrder` 加可空键字段；FK 链 + 000050
  IDENTITY 重写保证正常路径零 miss，miss 即结构化错误 fail-closed）；
  3 个 ENUM 列**不走应用侧解析**——行元组把同一 Python 文本值再 append 一
  次，由 ENUM 列类型赋值强制完成转换，文本↔ENUM 一致性由同源值构造保证，
  越界值 SQL 报错即 fail-closed（闭世界文法）。`ON CONFLICT DO UPDATE SET` 按
  "镜像文本列"规则追加 `basin_version_key`/`unit_e`/`quality_flag_e` 三列
  （文本侧 re-set 谁，代理侧就 re-set 谁的对应列；冲突键身份列两侧都不
  re-set）——这是 000050:244-247 预告的 drift 源的闭合；生产 DELETE-replace
  使该分支对既有行不触发，其验证走直接重放（design D3 可达性声明）。
- **`db/seeds/seed_demo.py`**（dev/demo 写者）：`_build_river_timeseries_rows`
  加键映射入参，行元组追加 7 列（seed 自身先插权威行，键可得；ENUM 同源
  值 + 列类型赋值强制），`ON CONFLICT DO NOTHING` 不变。
  seed 是唯一真实产生 `y_stage`/`m` 分支的写者，双写测试面覆盖该分支。
- **`RiverTimeseriesRow` 与 JSONL 产物零变化**（设计分叉显式裁决：键不
  进行 dataclass 行；`upsert_river_timeseries` 签名扩 keyword-only 键参
  随 Protocol 同步——`FileOutputParserRepository` 接受并忽略新参，JSONL
  逐字不变，两个测试 fake repository 同步扩签名——见 design D1）。
- 测试：unit（INSERT 形态/键传递/ON CONFLICT 镜像/fail-closed）+
  integration（真实 DB 双写端到端、run 域等值审计前后差值为零、直接重放
  INSERT 验镜像、与 #1339 回填 runner 共存——双写行不再是哨兵候选）。
- node-27 实机：完整 cycle 落库 + 新旧列一致性校验 + ingest 耗时对比
  （无现成计时钩子，证据用同 run 重解析 wall-clock 对比，不往生产代码
  加临时仪表——偏离记录）。

## Impact

- Affected specs: `river-identity-normalization`（ADDED requirement：写者双写）
- Affected code: `workers/output_parser/parser.py`、`db/seeds/seed_demo.py`、
  unit + integration 测试
- Not affected（non-goals，探索证据钉住）：
  `packages/common/forecast_store.py`、`services/tile_publisher/publisher.py`、
  `services/tile_publisher/forcing_copyback_backfill.py`——issue 列为疑似写者，
  实测全部只读（string 元数据/schema probe/SELECT-only），**不改**；
  读路径（#1341）、旧列下线、回填 runner、`timescale_write_guard`（守卫只看
  valid_time 窗口，双写不改守卫布线——issue 的"检查是否需同步更新"项以
  评审证据答复"不需要"）、OpenAPI/前端。
- node-27 实机：一个完整 cycle 的落库 + 一致性校验 + 耗时对比 receipt；
  不跑 enforce 全量回填（属 #1403 声明的后续运维窗口）。
