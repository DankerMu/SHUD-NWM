# Tasks: river_timeseries 写路径代理键双写（issue #1340）

## 1. 实现

- [x] 1.1 `workers/output_parser/parser.py`：`load_run_context` 追加
      3 键列（join 谓词按 design D1 钉死：bv=hydro_run 侧、rnv=
      model_instance 侧）；`load_river_segments` **主查询与 fallback 两条
      SELECT 都**追加 `river_segment_key`；`HydroRunContext` +3 键字段、
      `RiverSegmentOrder` +1 键字段（`int | None = None`，DB-free 路径留
      None）；`upsert_river_timeseries` 签名加 keyword-only 键参（design
      D1 定死），连带同步：Protocol（parser.py:142）、
      `FileOutputParserRepository`（:343，接受并忽略，JSONL 逐字不变）、
      `tests/test_output_parser.py:38`、`tests/test_e2e.py:280`，以及
      `parser.py:675` `_connection is None` 自委派分支同步转发新 kwargs；
      Psycopg 侧键 None/缺失 → 结构化错误 fail-closed（整批零写入）
- [x] 1.2 `upsert_river_timeseries` INSERT 列表 +7 列；行元组 arity
      10→17（ENUM 三值与文本同源 append，无 cast 无模板改动，design
      D2）；`ON CONFLICT DO UPDATE SET` 追加镜像三列（design D3，文本
      SET 列表逐字不变）
- [x] 1.3 `db/seeds/seed_demo.py`：`_build_river_timeseries_rows` 加键
      映射入参（纯函数形态保持），行元组 +7 列，`ON CONFLICT DO NOTHING`
      不变；键由权威表 **SELECT** 取回（不用 `RETURNING`——seed 权威行
      全是 DO NOTHING，重复 seed 时 RETURNING 为空），缺失即报错
- [x] 1.4 write-guard：`packages/common/timescale_write_guard.py` 零触碰；
      `tests/test_timescale_write_guard_wired.py` **断言逐字不变**（:263-268
      只看 INSERT 子串与行数，不看 arity）；但该文件 :215/:233/:248/:276
      直接调 `upsert_river_timeseries(_river_rows(), batch_size=N)`，新
      keyword-only 键参默认 None 会先撞 fail-closed——**调用点补键
      fixture**（终判迭代2 修正口径：碰撞在 upsert 入参，不在
      `_RecordingCursor` 的 load 响应）

## 2. 验证（Evidence Floor）

- [x] 2.1 unit：INSERT 形态（7 列 + arity 17 + ENUM 同源 + 镜像 SET）/
      load 查询含键列 / 键 None/缺失 fail-closed / 守卫顺序不变且无新增
      独立查询（design D7.1）；红证配对（pre-change 代码上新断言必红）
- [x] 2.2 integration（real-db marker）：双写端到端 7 列非 NULL + 等值
      审计**前后差值为零**；ON CONFLICT 经直接重放 INSERT 对预置漂移行
      触发并断言镜像（design D3 可达性声明；不覆盖锁竞争面）；与 #1339
      回填 runner 共存（旧形态行才是哨兵候选）；seed y_stage/m 分支
      （design D7.2）
- [x] 2.3 `uv run pytest -q` 定向（output_parser + write_guard_wired +
      seed 相关，`-m "not integration"` 55 passed）全绿；`uv run ruff
      check .` 通过。e2e 腿如实改述（round-1 裁决）：`-m e2e` 两例
      pre-existing 红（GFS `.idx` fixture 漂移，净 master 同红，另行
      立单），但 m2 的 ERA5 腿在 fail 前已真实驱动 `parse_run` 走新
      签名（parser.py:236/245 是唯一 parsed 路径）——签名兼容已被执行
      验证，e2e "全绿"声明撤回
- [x] 2.4 `openspec validate river-ts-writer-dual-write --strict
      --no-interactive` 通过
- [x] 2.5 diff 自证：`forecast_store.py`/`publisher.py`/
      `forcing_copyback_backfill.py`/读路径/回填 runner/
      `packages/common/timescale_write_guard.py` 零触碰；
      `RiverTimeseriesRow` 与 JSONL 产物零改动；文本列写入路径与
      DELETE-replace 谓词逐字节不变
- [ ] 2.6 node-27：完整 cycle（实际 basin 集合如实记录）落库；**run 域
      定向查询**（`WHERE run_id = <本次>`）7 列零 NULL + 等值审计**前后
      差值为零**（评审 round-1 P2-7：全表 verify 函数含 4.6 亿未回填
      历史行，不能按字面"零背离"；全表函数一次=249GB 扫，receipt 用
      定向 SQL）（AC-1/AC-4）
- [ ] 2.7 node-27：同 run 重解析 wall-clock 对比（旧 vs 新代码各一次，
      rows_written 相同）记录于 PR 评论；**前置：选中 run 的 valid_time
      窗口全落未压缩 chunk，并记录 chunk 压缩状态**（AC-2，design D5
      偏离口径）
- [ ] 2.8 node-27：定向真实 DB pytest（`-m integration` 本 change 子集）

## 3. 交付记录

- [ ] 3.1 PR body：偏离记录（issue 5 文件收窄为 2 真实写者 + "维表
      upsert"按 #1339 现实重映射 + 吞吐对比口径 + 17→18 流域）+ AC 逐条
      覆盖声明
- [ ] 3.2 PR body：node-27 cycle receipt 摘要 + 耗时对比数字
