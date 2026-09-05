# node-27 receipt — issue #2009 I5 全国径流周期目录（`cycles` / `valid-times`）

- 日期：2026-09-05（node-27 本机时间戳为 `+08:00`，下文 `2026-09-06T02:4x+08:00` 即 UTC 09-05 18:4x）
- 分支：`feat/issue-2009-discharge-cycles-catalog` ・ PR #2073 ・ epic #2003 (m27) ・ OpenSpec change `display-v2-national-timeline-precip-overlay` group 3（I5）
- **读数与 SHA 的对应**：全部读数取自受审终态 `5bd69f97`（Phase 7 终审 head）。node-27 上的树是 throwaway worktree `/home/nwm/tmp/wt-2073` 的本地 commit `1835d103` = origin/master `27dc6aab` + `git diff origin/master 5bd69f97` 补丁；
  `services/tiles/mvt.py`、`apps/api/routes/hydro_display.py`、`tests/test_hydro_display_mvt_scaling.py`、`tests/test_mvt_national_identity_probe_integration.py`、`openapi/nhms.v1.yaml` 五个文件 sha256 与本地 `5bd69f97` 逐一相同。
  第 5 节 SQL 行 mutation 取自门后 oracle pass 的 `db83a0c1`（`node27-run.log` 头部的 `HEAD=e5daedaf` 是当时 throwaway worktree 的本地 wip commit，其文件集 ≡ `db83a0c1`，见 `mutation-results/README.md`），两者之间生产代码与集成文件除一行 docstring 外零差异（`git diff --stat db83a0c1 5bd69f97 -- services apps openapi tests/test_mvt_national_identity_probe_integration.py`）。
- 节点：node-27（`210.77.77.27`），active primary PG `:55432`
- 执行方式：**未动生产**。生产 `nhms-display-api.service`（:8080，`/home/nwm/NWM` 仍在 master `f14edc0e`）与 `https://test.nwm.ac.cn` 全程未重启、未 `git pull`；
  「改后」侧是从 `wt-2073` 另起的单 worker uvicorn `127.0.0.1:8090`，`set -a; . infra/env/display.env`（同一 `nhms_display_ro` 角色）+ `PYTHONPATH=/home/nwm/tmp/wt-2073` + 生产解释器 `/home/nwm/NWM/.venv/bin/python`（`uv.lock`/`pyproject.toml` 在 `master..origin/master` 无差异，故解释器等价且不触发 resync）+ `NHMS_MVT_FILE_CACHE_DIR` 指向本次专用空目录。
  判别器：`GET /api/v1/layers/discharge/cycles?source=gfs` 在 :8080 为 404、在 :8090 为 200；`/api/v1/runtime/config` 报 `service_role=display_readonly`。两侧各先打 3 次不计数的 `x-nhms-cache-warm: refresh` 预热，再采样（预热在外层 runner `/home/nwm/tmp/run-2073-receipts.sh` 里，不在 receipt 脚本本身；
  runner 全文、`before2` 对照与首轮读数的终端捕获、`45.52s` 的集成日志尾都归档在 `.workplans/pr-2073/phase8/node27-receipts-raw.md`）。
- 数据库连接串一律脱敏为 `postgresql://nhms:***@127.0.0.1:55432/nhms`（从 `infra/env/*.env` 远端读取，未回显）。

## 1. 真实 DB 集成测试（终态树 `1835d103` ≡ `5bd69f97`）

```
NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:***@127.0.0.1:55432/nhms \
TMPDIR=/home/nwm/tmp PYTHONPATH=/home/nwm/tmp/wt-2073 \
/home/nwm/NWM/.venv/bin/python -m pytest tests/test_mvt_national_identity_probe_integration.py -q -p no:cacheprovider
→ 15 passed in 45.52s   # 2026-09-06T02:45:55+08:00
```

15 = 既有 8 条 `{source}/{cycle}` 瓦片用例（含旧 5 段全国路由三条回归）+ 本 PR 新增 7 条：
`test_national_cycles_list_only_cycles_covered_by_every_network`、`…_valid_times_are_empty_for_a_cycle_outside_the_intersection`、`…_cycles_ignore_an_inactive_network`、`…_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first`、
`…_cycles_skip_a_cycle_older_than_the_lookback`、`…_cycles_match_an_uppercase_source_id_from_a_lowercase_query`、`…_cycles_take_the_newest_run_at_a_cycle`（第二流域用独立 `core.basin_version` + 逐小时 seed，per-test throwaway DB）。
门后 oracle pass 树（`db83a0c1` 文件集）上同一命令曾 `15 passed in 34.78s`（`2026-09-06T01:03:02+08:00`）。

第二个集成文件（同一 throwaway worktree、同一解释器、同一 URL）：

```
… -m pytest tests/test_river_ts_read_path_surrogate_keys_integration.py -q -p no:cacheprovider
→ 14 passed in 431.21s (0:07:11)   # 2026-09-06T03:1x+08:00，supp 段
```

## 2. `GET /api/v1/layers` 冷 p95（验收口径：绝对上限 500 ms，且同一 receipt 内 `after − before ≤ 50 ms`）

同会话、同方法：`curl -H "x-nhms-cache-warm: refresh"`，12 次采样，`sort -n` 取第 ⌈0.95·n⌉ 位；before 与 after 背靠背（相隔 1 s）。

| 侧 | 进程 | n | p95 | median | max | min |
|---|---|---|---|---|---|---|
| before | :8080 生产 master `f14edc0e` | 12 | **0.091 s** | 0.063 s | 0.091 s | 0.057 s |
| after | :8090 `1835d103`（≡ `5bd69f97`） | 12 | **0.069 s** | 0.052 s | 0.069 s | 0.051 s |
| before（after 之后再采一组的对照） | :8080 | 12 | 0.091 s | 0.081 s | – | – |

- 上限：0.069 s ≤ 0.5 s ✅。回归子句：`after − before = −22 ms ≤ 50 ms` ✅（两次对照都不比 after 快，差值不是采样噪声方向的巧合）。
- 同一脚本更早一次运行（`02:39`，body 解析段因 JSON 信封路径写错而报 KeyError，但 p95 段有效）：before 0.110 s / after 0.073 s / 对照 0.097 s，结论相同。
- 注意生产侧 2 worker、改后侧 1 worker；单请求串行冷采样不受 worker 数影响，但改后侧没有 warmer 后台负载，差值方向对 after 略有利，故不据此宣称「更快」，只据此宣称「未回归」。

## 3. 目录 / 周期 / 有效时间实机 body（:8090，均 `refresh`）

**discharge 条目，runless 与 `?run_id=<最新 display-ready run>` 背靠背、都带 refresh**：

```
{"default_source": "gfs", "default_cycle": "2026-09-04T12:00:00Z", "version": null} valid_times 56 template /api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf
{"default_source": "gfs", "default_cycle": "2026-09-04T12:00:00Z", "version": null} valid_times 56 template /api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf
runless == run-scoped (byte-identical)
```

**`cycles?source=gfs` / `?source=ifs`**（交集列表，最新在前）：

```
gfs len 18 default 2026-09-04T12:00:00Z first [{cycle_time 2026-09-04T12:00:00Z, valid_time_start 2026-09-04T12:00:00Z, valid_time_end 2026-09-11T09:00:00Z}] last [{cycle_time 2026-08-27T00:00:00Z, … valid_time_end 2026-09-02T21:00:00Z}]
ifs len 18 default 2026-09-04T12:00:00Z first [同上] last [同上]
```

- fail-closed 交集在实机上可见：`hydro_run` display-ready 前沿是 `2026-09-05 00:00Z`（第 4 节），但 `default_cycle` 停在 `2026-09-04T12Z`——09-05T00Z 周期尚未被全部 38 个活跃河网覆盖，目录不广告它。
- 决策 15 / matrix row 36：最旧 `cycle_time = 2026-08-27T00Z`，距采样时刻（09-05 18:43Z）9.8 天 < 12 天 ✅。

**`valid-times?source=gfs&cycle=2026-09-04T12:00:00Z`**：

```
n 56 first ['2026-09-04T12:00:00Z'] last ['2026-09-11T09:00:00Z'] observed 56 truncated False
HTTP 200 0.013403s
```

56 项 = `[C, min(river_valid_time_end)]` 上 3 h 步长（09-04T12Z → 09-11T09Z 共 165 h / 3 + 1）。

**无参 `valid-times`（refresh）**：`HTTP 200 0.088793s`（覆盖行数见第 4 节「no-arg (unbounded) 2250」）。

**422 形状**（路由层拒绝，未执行 SQL）：`cycle=…`（缺 source）→ 422；`source=gfs`（缺 cycle）→ 422；`source=ERA5&cycle=…` → 422；`cycles?source=ERA5` → 422。

**gfs / ifs 同一 `(cycle, valid_time)` 各一张 z4 瓦片**（`cycle=2026-09-04T12:00:00Z`，`valid_time=2026-09-05T00:00:00Z`，z4/x12/y6，专用空文件缓存，冷→热）：

| 源 | 冷 | 热 | bytes | `X-Tile-Cache-Key` |
|---|---|---|---|---|
| gfs | 200，11.63 s | 200，0.040 s | 1 374 288 | `19e12a79…be2ef99` |
| ifs | 200，13.26 s | 200，0.038 s | 1 374 324 | `3971c5a0…8fac5a0` |

cache key 不同、字节非空、两张 body 不同（`cmp` 不等），ETag 也不同（`W/"m16-1e1fd2…"` vs `W/"m16-4f1d5e…"`，内容观测）；文件缓存写入 4 条。冷 11–13 s 是 z4 全国瓦片的 live PostGIS 路径（pre-existing，与本 PR 无关；生产由 prewarm 覆盖）。

**424（该源该周期无 run）**：`gfs` `cycle=2026-09-05T06:00:00Z`（窗口内、无任何 run）→ 424；`gfs` `cycle=2026-09-05T00:00:00Z`（display-ready 前沿，部分河网有 run、未入交集）→ 200——瓦片路由是 per-source fail-closed（#2007），交集 fail-closed 只在目录层，两者行为一致于各自 spec。

**瓦片路由回归**：旧 5 段 alias `/api/v1/tiles/hydro-national/q_down/2026-09-04T12:00:00Z/4/12/6.pbf` → 200；canonical `/api/v1/tiles/hydro-national/gfs/2026-09-04T12:00:00Z/q_down/2026-09-04T12:00:00Z/4/12/6.pbf` → 200。

## 4. DB receipts（`nhms_display_ro`，采样时刻 2026-09-05 18:43Z）

| 量 | 值 |
|---|---|
| 交集分母 (a) `count(DISTINCT river_network_version_id) FROM core.model_instance WHERE active_flag AND river_network_version_id IS NOT NULL` | 38 |
| `run_display_coverage` 中 `segment_count = 0` 行数（matrix row 3 的 oracle 前提，非 0 才有意义） | 911 |
| 覆盖查询返回行数 (b)，gfs、12 天窗口、`rn = 1` | 800 |
| 覆盖查询返回行数，无参（不加 `:since`，不加 source） | 2250 |
| `len(cycles)` / `default_cycle`（gfs 与 ifs 相同） | 18 / `2026-09-04T12:00:00Z` |
| `run_display_coverage` 前沿 `max(refreshed_at)` | 2026-09-05 18:42:27Z |
| `hydro_run` display-ready 前沿 `max(cycle_time)` | 2026-09-05 00:00Z |

覆盖新鲜度基线（给 #2080 起点）：coverage 表在采样前 1 min 刚刷新过，而 display-ready 最新周期 09-05T00Z 的交集尚未闭合——差的不是 coverage 刷新滞后，而是部分河网 09-05T00Z 的 run 还没 display-ready。

**`EXPLAIN (ANALYZE, BUFFERS)` 覆盖语句（gfs，12 天）**，顶层 `Sort` 节点 actual 11.8 ms、800 行（脚本 `head -12` 截掉了 `Execution Time` 行，总耗时略高于 11.8 ms，未记）。
**披露**：脚本里的语句是手抄的覆盖查询，把应用 SQL 的 `(CAST(:since AS timestamptz) IS NULL OR h.cycle_time >= :since)` / `(CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)` 拍平成裸谓词、投影列更少（应用原文见 `services/tiles/mvt.py:2016-2050`）；
psycopg2 客户端内插常量后 PG 会把 `Const IS NULL OR …` 常量折叠成同样的裸谓词，计划形状与行数不受影响，但严格说这是「等价语句的计划」而非应用语句原文的计划，后续 receipt 应直接 EXPLAIN 应用原文：

```
Sort (actual time=11.752..11.804 rows=800)
  -> Subquery Scan on r -> WindowAgg (rows=800)
     -> Sort -> Nested Loop (actual time=4.177..9.038 rows=800)
        -> Hash Join (rows=800)
           -> Seq Scan on model_instance mi (rows=38)
           -> Hash <- Index Scan using hydro_run_display_ready_candidate_idx on hydro_run h (actual time=0.081..3.743 rows=800)
                Index Cond: ((lower(source_id) = 'gfs'::text) AND (cycle_time >= (now() - '12 days'::interval)))
        -> Index Scan using run_display_coverage_pkey on run_display_coverage rdc (rows=1 loops=800)
             Index Cond: (run_id = h.run_id)
```

- `:since` 谓词确实进入 Index Cond（决策 15 的「界住 DB 扫描」在计划层可见）。
- **走的是 `hydro_run_display_ready_candidate_idx`（migration 000040），不是矩阵决策 15 bullet（及 PR body 证据表同一行）预写的 `hydro_run_latest_ready_run_idx`（000021）**。前者是 `#2007` 之后为 display-ready 候选加的索引，条件更贴合本语句；Evidence Floor 行的索引名是预写错了，已按实测改写，语义（索引扫描、非 seq scan、`:since` 入 Index Cond）不变。

## 5. 突变矩阵的真实 DB 行（`db83a0c1` 文件集，`/home/nwm/tmp/wt-2073`；驱动 `node27-mutate.py`，日志归档为 `.workplans/pr-2073/review/mutation-results/node27-run.log`）

每行：施加单个 mutation → 跑集成文件 → 恢复；`file:line` 由驱动按首个变更行生成。

| row | 站点 | 结果 | 变红用例 |
|---|---|---|---|
| 3 | `services/tiles/mvt.py:2041`（`AND rdc.segment_count > 0` 在 JOIN ON） | 1 failed, 14 passed | `…keep_a_cycle_whose_zero_segment_rival_run_sorts_first` |
| 36 | `mvt.py:2047`（`:since` 谓词） | 1 failed, 14 passed | `…skip_a_cycle_older_than_the_lookback` |
| 50 | `mvt.py:2004`（active-network `active_flag`） | 1 failed, 14 passed | `…ignore_an_inactive_network` |
| 51 | `mvt.py:2005`（`river_network_version_id IS NOT NULL`） | 15 passed（**tripwire-only**） | – ：`core.model_instance.river_network_version_id` 是 `TEXT NOT NULL`（`000004_core.sql:74`），NULL 行在 schema 上不可构造，该谓词无真实 oracle，fixture 照实写 |
| 52 | `mvt.py:2034`（`PARTITION BY river_network_version_id, cycle_time`） | 1 failed, 14 passed | `…list_only_cycles_covered_by_every_network` |
| 53 | `mvt.py:2035`（`ORDER BY run_id DESC`） | 1 failed, 14 passed | `…take_the_newest_run_at_a_cycle` |
| 54 | `mvt.py:2045`（`lower(h.source_id) = :source`） | 1 failed, 14 passed | `…match_an_uppercase_source_id_from_a_lowercase_query` |
| 55 | `mvt.py:2046`（`:cycle` 谓词） | 1 failed, 14 passed | `…valid_times_are_empty_for_a_cycle_outside_the_intersection` |
| 56 | `mvt.py:2049`（`rn = 1`） | 1 failed, 14 passed | `…take_the_newest_run_at_a_cycle` |

本地 83 行矩阵（`mutate4`，基线 `local 372 passed`）82 行变红；当时唯一本地恒绿的 row 3 由上表补齐（round 4 之后 row 3 又加了 `segment_count > 0` 文本 tripwire，矩阵 Measured 格现为 `local 371 pass / 1 fail`，但按 fixture 前言 tripwire 不算 oracle，node-27 这一行仍是它唯一的 oracle）。

## 6. 生效 retention 与决策 15 不等式

- `infra/env/node27-raw-retention.env`（gitignored，仅存在于活动 checkout；receipt 脚本在 `wt-2073` 里 grep 不到它，本值取自 supp 段对活动 checkout 的直接 grep）：`NODE27_RAW_RETENTION_DAYS=14`；源默认 `DEFAULT_RETENTION_DAYS = 14`（`scripts/node27_raw_retention.py`）；登录环境 `NODE27_RAW_RETENTION_DAYS=<unset>`。
- `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS = 12`（`services/tiles/mvt.py:128`）：`L = 12 ≤ R − 1 = 13` ✅。

## 7. 覆盖到的 Evidence Floor 项（`tasks.md` 第 3 组的 node-27 项）

| 项 | 本 receipt |
|---|---|
| 两个集成文件 `NHMS_RUN_INTEGRATION=1 … pytest tests/test_mvt_national_identity_probe_integration.py tests/test_river_ts_read_path_surrogate_keys_integration.py` | §1：15 passed + 14 passed（Evidence Floor 写的是一条命令带两个文件，实际分两次调用、同环境；throwaway worktree，生产解释器）。**未做**：活动 checkout `/home/nwm/NWM` 上的同一命令——那要先 `git pull --ff-only`，归合并后的部署段，见 §8 |
| gfs/ifs 同一 cycle/valid_time 各一张 z4 瓦片：`X-Tile-Cache-Key` 不同、字节非空 | §3：两 key 不同、1 374 288 / 1 374 324 bytes、body 不同 |
| 某源该周期无 run → 424；旧路由仍 200 | §3：`2026-09-05T06Z` → 424；alias / canonical 200 |
| cycles 端点返回交集列表；valid-times 返回 clamp 后的 3 h 列表 | §3：18 项 / 56 项（`tasks.md` 原写 57 项 = 矩形 +0h…+168h 的情形，已按 D2 clamp 口径改写） |
| 瓦片冷/热耗时 | §3：gfs 11.63 s → 0.040 s，ifs 13.26 s → 0.038 s |
| `GET /api/v1/layers` 冷 p95 改前/改后各 ≥10 次、同会话同方法、500 ms 上限 + `after − before ≤ 50 ms` | §2 |
| 交集分母 / 覆盖行数 / `len(cycles)` / `default_cycle` | §4 |
| runless 与 `?run_id=` 背靠背一致 | §3 |
| row 36 / 决策 15：cycle 都在 12 d 内、EXPLAIN | §3、§4 |
| row 3 前提 `segment_count = 0` 计数 | §4：911 |
| coverage 新鲜度基线（#2080） | §4 |
| 无参 valid-times 行数与耗时 | §3、§4 |
| 生效 retention 与决策 15 不等式 | §6 |
| 7 条新集成用例 node-27 跑绿 | §1 |
| SQL 行 mutation 在 node-27 测红 | §5 |

## 8. 清理

- :8090 uvicorn 已 kill（`pgrep -f "port 8090"` 为 0），专用 `NHMS_MVT_FILE_CACHE_DIR` 已删除。
- throwaway worktree `/home/nwm/tmp/wt-2073`（本地 commit 从未 push）在 PR 合并、活动 checkout `git pull --ff-only` 之后 `git worktree remove`。
- 生产 :8080 在本 receipt 期间未变更；合并后的部署（`git pull --ff-only` + `scripts/ops/start-display-api.sh`）另记。
