# node-27 receipt — issue #2007 hydro-national `{source}/{cycle}` 瓦片

- 日期：2026-09-04
- 分支：`feat/issue-2007-hydro-national-source-cycle-route`
- **读数与 SHA 的对应**（不要混看）：第 1、2 节取自 `6c30e33d`（交叉审查前，基 master `70337533`）；
  第 3 节 (a)(b)(c) 取自 round-1 修复后的 `9ac6aaf3`；第 3 节 (d) 取自 round-2 修复后的 `cdfbc3d3`；
  第 4 节的突变矩阵取自 rebase 到 master `d812def6` 并完成 invariant-closure 修复后的树。
  `6c30e33d` → `cdfbc3d3` 之间生产代码逐字未变（`git diff --stat 6c30e33d cdfbc3d3 -- services apps` 为空），
  故第 2 节的实机读数对后续 SHA 仍然成立；rebase 引入的 #2005 改动只落在 `river-network-national` 分支。
- PR：#2027 ・ epic #2003 (m27) ・ OpenSpec change `display-v2-national-timeline-precip-overlay` group 3
- 节点：node-27（`210.77.77.27`），active primary PG `:55432`
- 执行方式：**未动生产**。在 `/home/nwm/wt-2007` 新建 git worktree，独立 `uv sync`，另起 uvicorn 于 `127.0.0.1:8090`；生产 `nhms-display-api.service`（:8080）与 `https://test.nwm.ac.cn` 全程未重启、未切分支。
- 运行环境：`DATABASE_URL` 角色 `nhms_display_ro`，驱动 **psycopg2**，`NHMS_ENABLE_LIVE_POSTGIS_MVT=true`，`NHMS_MVT_FILE_CACHE_DIR` 指向本次专用空目录（冷读数才真实）。
- Python：3.11.15（worktree 自建 venv）

## 1. 真实 DB 集成测试

```
NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:***@127.0.0.1:55432/nhms \
TMPDIR=/home/nwm/tmp uv run pytest tests/test_mvt_national_identity_probe_integration.py -q
→ 5 passed in 13.98s   # @6c30e33d
```

这 5 项是 `6c30e33d` 时的用例集。此后各轮交叉审查持续补 oracle，该文件在最终树上是 **8 项**，
与 `tests/test_river_ts_read_path_surrogate_keys_integration.py` 合跑的计数见第 3 节 (a)(d) 与第 4 节。

5 项 = 旧 5 段路由的 3 条既有用例（未改语义，本次同时是旧路由行为不变的回归 oracle）+ 本 issue 新增 2 条：

- 跨源 fail-closed：同一 cycle 只有 gfs 有 display-ready run 时，`source=gfs` 得 200 且含种子要素，`source=ifs` 得 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`（`details` 带 z/x/y、无 `required_env`，与 `_require_live_postgis_mvt` 的同码 424 区分开）。
- 大小写：run 存为 `source_id = 'IFS'` 时，小写路径段 `ifs` 得 200；同周期 `gfs` 无 run 故 424。

每条用例用 per-test throwaway database，未触碰任何活库。

## 2. 实机瓦片 receipt（z4 / x12 / y6，覆盖中国主体）

取数方式（`/home/nwm/run-2007-receipt.sh`，独立 uvicorn `127.0.0.1:8090`，空文件缓存目录）：

```bash
curl -s -o body.bin -D hdr.txt -w '%{http_code} %{size_download} %{time_total}' \
  "$B/api/v1/tiles/hydro-national/gfs/2026-09-03T00:00:00Z/q_down/2026-09-03T12:00:00Z/4/12/6.pbf"
# ETag / X-Tile-Cache / X-Tile-Cache-Key 从 hdr.txt 取；冒号与 + 在路径段里按 %3A / %2B 转义
```

数据前提（实测），核查语句：

```sql
SELECT h.source_id, h.cycle_time, count(DISTINCT mi.river_network_version_id)
FROM hydro.hydro_run h
JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
WHERE h.status IN ('succeeded','parsed','published') AND mi.active_flag
GROUP BY 1, 2 ORDER BY 2 DESC, 1;
```

`hydro.hydro_run.source_id` 实机只有 `gfs`（小写）与 `IFS`（大写）两种拼写；周期 `2026-09-03T00:00:00Z` 上 gfs 与 ifs **各 38/38** 河网 display-ready，`2026-09-03T03:00:00Z` 零 run。

| 用例 | HTTP | 字节 | 秒 | cache | cache key 前 16 位 | ETag 前 16 位（`W/"m16-…"`） |
|---|---|---|---|---|---|---|
| 新路由 gfs 冷 | 200 | 1302580 | 2.566 | miss | `57b487e5a9d5403f` | `636f58c3cb6c0424` |
| 新路由 gfs 热 | 200 | 1302580 | 0.038 | hit | `57b487e5a9d5403f` | `636f58c3cb6c0424` |
| 新路由 ifs 冷（同 cycle/valid_time） | 200 | 1302548 | 1.728 | miss | `6c41039807ddcf7f` | `c5573bfba50d0b5c` |
| 新路由 ifs 热 | 200 | 1302548 | 0.033 | hit | `6c41039807ddcf7f` | `c5573bfba50d0b5c` |
| 旧 5 段路由 冷 | 200 | 1302548 | 1.685 | miss | `aba7d520afbaae84` | `c5573bfba50d0b5c` |
| 旧 5 段路由 热 | 200 | 1302548 | 0.052 | hit | `aba7d520afbaae84` | `c5573bfba50d0b5c` |

- **两源确实分流**：同一 cycle/valid_time/z/x/y 下 gfs 与 ifs 的字节数、ETag、cache key 三者全部不同。ETag 之所以不同是因为字节不同（`stable_etag` 只哈希瓦片字节），不是因为身份进了 ETag。
- **旧路由行为不变**：旧路由的字节与 ETag 与 **ifs** 那张完全一致（`m16-c5573bf…`）。旧路由按 `cycle_time DESC, run_id DESC` 取每个河网的最新 run，在该周期上选中的就是 IFS 一侧——这正是改前的混源行为，说明 NULL 绑定确实让旧路由的选 run 未被收窄。它的 cache key 与两条新路由都不同（`-v5` 版本轮换后的新 key，属预期的一次性缓存失效）。

### fail-closed（周期无 run）

| 用例 | HTTP | error.code |
|---|---|---|
| `gfs` @ `2026-09-03T03:00:00Z` | 424 | `MVT_LIVE_POSTGIS_UNAVAILABLE` |
| `ifs` @ `2026-09-03T03:00:00Z` | 424 | `MVT_LIVE_POSTGIS_UNAVAILABLE` |

**偏差说明**：Epic 验收项写「某源该周期无 run 时 424」。实机上每个周期 gfs 与 ifs 都是 38/38 完整，**不存在**一源有、另一源无的天然周期，因此实机 424 用的是零 run 的周期。「一源有、另一源无」这一条由第 1 节的 throwaway-DB 集成用例确定性覆盖（可按需复现），不靠生产数据碰运气。

### 校验 422（未执行任何 SQL）

| 用例 | HTTP | 秒 | error.code |
|---|---|---|---|
| `source=ERA5` | 422 | 0.0043 | `VALIDATION_ERROR`（`path.source`） |
| `source=best` | 422 | 0.0045 | `VALIDATION_ERROR`（`path.source`） |
| `cycle=not-an-instant` | 422 | 0.0036 | `VALIDATION_ERROR`（`path.cycle`） |
| `cycle=2026-09-03T00:00:00.500Z` | 422 | 0.0040 | `VALIDATION_ERROR`（秒精度） |

四条均在 4 ms 量级返回，对比冷瓦片 1.7–2.6 s，佐证校验早于任何 SQL。

### 时间实例拼写归一

| 拼写 | HTTP | cache | cache key 前 16 位 |
|---|---|---|---|
| `2026-09-03T00:00:00Z` | 200 | miss→hit | `57b487e5a9d5403f` |
| `2026-09-03T00:00:00.000Z` | 200 | hit | `57b487e5a9d5403f` |
| `2026-09-03T00:00:00+00:00` | 200 | hit | `57b487e5a9d5403f` |

三种拼写命中同一条缓存，未写出第二份瓦片。

## 3. 交叉审查各轮的实机验证（(a)(b)(c) @ `9ac6aaf3`，(d) @ `cdfbc3d3`）

round-1 交叉审查发现两个覆盖洞与一条实际破坏，均在 node-27 上实测确认并复验：

**（a）既有真实 DB 测试被新绑定打断（已修）**

`tests/test_river_ts_read_path_surrogate_keys_integration.py` 把 `hydro-national` 的 `source_rows` CTE 切出来直接执行，新增绑定未同批供参：

| 阶段 | 结果 |
|---|---|
| 修复前 | `2 failed, 12 passed` — `StatementError: A value is required for bind parameter 'source'` |
| 修复后 | 与 probe 文件合跑 `21 passed in 50.44s` |

**（b）突变实验：修复前两个核心谓词形同虚设，修复后咬住**

在 node-27 worktree 里删掉谓词再跑真实 DB 集成测试。统一流程（每条突变后还原并核对 `git diff` 为空）：

```bash
cd /home/nwm/wt-2007 && cp services/tiles/mvt.py /tmp/mvt.bak
uv run python /home/nwm/mut27.py <mutation-name>   # 先按行内容断言再删除/改写，绝不按行号盲改
NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... TMPDIR=/home/nwm/tmp \
  uv run pytest tests/test_mvt_national_identity_probe_integration.py -q
cp /tmp/mvt.bak services/tiles/mvt.py && git diff --stat -- services/tiles/mvt.py   # 必须为空
```


| 突变 | 修复前 | 修复后 |
|---|---|---|
| 删掉全部 `h.cycle_time = :cycle` 谓词（3 处） | **5 passed**（只有字符串形状断言变红） | **1 failed** — `test_national_identity_tile_serves_the_requested_cycle_not_the_newest_one` |
| 只删数据 CTE 的身份谓词（保留探针与 digest） | **5 passed** | **2 failed** — 上述用例 + `..._serves_the_requested_source_not_the_other_one_at_that_cycle` |

第二行的修复前状态正是本 issue 要消灭的「同一张图 gfs/IFS 混源」：探针答 200、CTE 画另一个源。判别器用 tile 的 `run_id` 属性——既有断言只看 segment/network id，两个 run 在这两项上完全相同，因此看不出差别。

**（c）修复后新增的 RFC3339 形状门实机复测（HEAD `9ac6aaf3`，独立端口 :8091）**

第 2 节的 422 表是在加形状门**之前**（`6c30e33d`）读的，而形状门恰好改写了那张表覆盖的面，故在修复后的 HEAD 上重测：

| 用例 | HTTP | 秒 | 说明 |
|---|---|---|---|
| `cycle=1756814400`（Unix epoch） | 422 `VALIDATION_ERROR` | 0.014 | 加门前是 200，且绑定到 2025-09-02T12:00Z |
| `cycle=2026-09-03T00:00:00`（无偏移） | 422 `VALIDATION_ERROR` | 0.006 | 加门前 200 |
| `cycle=2026-09-03 00:00:00`（空格分隔） | 422 `VALIDATION_ERROR` | 0.004 | 加门前 200 |
| `cycle=9999-12-31T23:59:59-08:00` | 422 `VALIDATION_ERROR` | 0.004 | 加门前 500（`OverflowError`） |

仍必须接受的拼写，全部 200 且**命中同一条缓存**（`X-Tile-Cache-Key` 前 16 位 `57b487e5a9d5403f`，与第 2 节同值）：`...T00:00:00Z`（冷 2.053 s）、`.000Z`、`+00:00`、`2026-09-03T08:00:00+08:00`（同一时刻的非 UTC 偏移，热 0.037 s）。

旧 5 段路由不受形状门影响：200，key `aba7d520afbaae84`。

**（d）round-2 补 oracle 后的突变复测（HEAD `cdfbc3d3`）**

round-2 交叉审查发现两条「代码对、但没人看得住」的洞：路由向 `national_discharge_source_version`
传身份这件事无人断言；身份探针的 `:cycle` 半边只有字符串形状断言。补 oracle 后复测：

| 阶段 | 结果 |
|---|---|
| 基线（两个集成文件合跑） | `21 passed in 543.44s` |
| 只删探针的 `:cycle` 谓词（数据 CTE 与 digest 的同名谓词保留） | `assert 200 == 424`，`1 failed, 6 passed` — `test_national_identity_tile_serves_the_requested_cycle_not_the_newest_one` |
| 还原后 | `git diff` 为空，4 处 `cycle_time = :cycle` 齐全 |

这条突变在 round-2 之前是**全绿**的：既有身份用例要么把 `:source` 绑到一个没有 run 的源
（`:source` 半边独立成立即可 424），要么请求的 cycle 本来就有 run。新增的 `_PRUNED_CYCLE_TIME`
请求（一个从未 seed 过的 cycle）是探针 `:cycle` 半边当时唯一的行为级 oracle。

## 4. 突变矩阵的真实 DB 行（rebase 到 `d812def6` + invariant-closure 修复后）

三轮交叉审查全部 not-clean，触发三轮硬闸。持久化的 Review Failure Retro（形状 `depth`）判定根因是
修复提示词太窄——每轮只补被点名的洞。纠正动作是 **invariant closure retry**：枚举每条行为声明、
写出最小破坏突变与必须变红的测试，验收门是**整张矩阵都得红**。完整 26 行矩阵在
`openspec/changes/display-v2-national-timeline-precip-overlay/invariant-matrix-i4-2007.md`
的 `## Mutation matrix`；本节是其中必须在真实 PG 上跑的那些行。

执行方式：`/home/nwm/wt-2007`（node-27），每条突变由 `/home/nwm/mut27.py` 按**行内容断言**后改写，
跑完还原。基线与终态的 `md5sum` 一致，且与本地文件逐字相同
（`services/tiles/mvt.py` `3c88decc…`、`apps/api/routes/hydro_display.py` `69edabcb…`），
证明所有突变都被完整撤销。

```
基线：tests/test_mvt_national_identity_probe_integration.py
      + tests/test_river_ts_read_path_surrogate_keys_integration.py
      -> 22 passed in 48.64s
```

| 矩阵行 | 突变 | 结果 | 变红的用例 |
|---|---|---|---|
| 1 | 删掉数据 CTE 的身份对 | 2 failed, 6 passed | `..._serves_the_requested_cycle_not_the_newest_one`、`..._serves_the_requested_source_not_the_other_one_at_that_cycle` |
| 2 | 删掉身份探针的身份对 | 3 failed, 5 passed | `..._is_424_for_the_source_without_a_run_at_that_cycle`、`..._matches_an_uppercase_source_id_from_a_lowercase_path`、`..._serves_the_requested_cycle_...` |
| 3 | 只删探针的 `:cycle` 半边 | 1 failed, 7 passed | `..._serves_the_requested_cycle_not_the_newest_one`（`_PRUNED_CYCLE_TIME` 变空 200） |
| 4 | 三处 `AND (` → `OR  (`（合取变析取） | 5 failed, 3 passed | 上述四条 + `test_national_digest_narrows_the_ranked_runs_to_the_bound_identity` |
| 5 | 三处去掉 `lower()` | 2 failed, 6 passed | `..._matches_an_uppercase_source_id_...`、`..._serves_the_requested_source_...` |
| 6 | 三处 `h.cycle_time = :cycle` → `<=` | 1 failed, 7 passed | `..._serves_the_requested_cycle_...`（靠 `_UNLANDED_CYCLE_TIME`：一个尚未发布的 cycle，`<=` 会让最新 run 冒充它。仅靠 `_PRUNED_CYCLE_TIME` 分辨不出，它比所有 run 都旧，`=` 与 `<=` 都选不中） |
| 7 | 删掉 digest 排名子查询的身份对 | 1 failed, 7 passed | `test_national_digest_narrows_the_ranked_runs_to_the_bound_identity`（本仓第一条真正**执行**收窄后 digest 的测试） |
| 11 | 旧路由改绑 `source="gfs"` | 1 failed, 7 passed | `..._matches_an_uppercase_source_id_from_a_lowercase_path` 里旧路由在 `_IFS_WINDOW_END` 的那次请求——该时刻只有 IFS run 有 coverage，旧路由一旦开始过滤就 424 |
| 12 | 去掉 NULL 保护（留下裸谓词） | 4 failed, 4 passed | 三条改前既有的旧路由用例之一 + 三条身份用例；旧路由绑 NULL 后谓词成 `NULL = NULL`，200 全变 424 |

矩阵中其余 15 行在本地单测层结算（其中 14 条由实现者逐条实测），见 `invariant-matrix-i4-2007.md`。

## 5. 覆盖到的 Evidence Floor 项

group 3 Evidence Floor 中属于本 issue 的实机项全部满足：新路由 gfs/ifs 同 cycle 各一张 z4 瓦片（200、字节非空、`X-Tile-Cache-Key` 不同、ETag 因字节不同而不同）、无 run 时 424、旧路由仍 200、冷/热耗时已记录。`cycles` 端点与 57 项 valid-times 属 I5/#2009，不在本 receipt。

## 6. 清理

`/home/nwm/wt-2007` worktree、`/home/nwm/tmp/mvt-cache-2007*` 缓存目录、`/home/nwm/run-2007-*.{sh,log}`、`/home/nwm/run-r2*.{sh,log}`、`/home/nwm/run-matrix27*.{sh,log}`、`/home/nwm/mut27.py`、`/home/nwm/*-2007.patch` 与 `/home/nwm/{mv,hd}.*.bak` 在合并后移除；生产服务与 `/home/nwm/NWM` 工作树自始至终停留在 master。
