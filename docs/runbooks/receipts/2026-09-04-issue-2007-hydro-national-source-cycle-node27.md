# node-27 receipt — issue #2007 hydro-national `{source}/{cycle}` 瓦片

- 日期：2026-09-04
- 分支 / HEAD：`feat/issue-2007-hydro-national-source-cycle-route` @ `6c30e33d`（rebase 到 master `70337533` 之后）
- PR：#2027 ・ epic #2003 (m27) ・ OpenSpec change `display-v2-national-timeline-precip-overlay` group 3
- 节点：node-27（`210.77.77.27`），active primary PG `:55432`
- 执行方式：**未动生产**。在 `/home/nwm/wt-2007` 新建 git worktree，独立 `uv sync`，另起 uvicorn 于 `127.0.0.1:8090`；生产 `nhms-display-api.service`（:8080）与 `https://test.nwm.ac.cn` 全程未重启、未切分支。
- 运行环境：`DATABASE_URL` 角色 `nhms_display_ro`，驱动 **psycopg2**，`NHMS_ENABLE_LIVE_POSTGIS_MVT=true`，`NHMS_MVT_FILE_CACHE_DIR` 指向本次专用空目录（冷读数才真实）。
- Python：3.11.15（worktree 自建 venv）

## 1. 真实 DB 集成测试

```
NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:***@127.0.0.1:55432/nhms \
TMPDIR=/home/nwm/tmp uv run pytest tests/test_mvt_national_identity_probe_integration.py -q
→ 5 passed in 13.98s
```

5 项 = 旧 5 段路由的 3 条既有用例（未改语义，本次同时是旧路由行为不变的回归 oracle）+ 本 issue 新增 2 条：

- 跨源 fail-closed：同一 cycle 只有 gfs 有 display-ready run 时，`source=gfs` 得 200 且含种子要素，`source=ifs` 得 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`（`details` 带 z/x/y、无 `required_env`，与 `_require_live_postgis_mvt` 的同码 424 区分开）。
- 大小写：run 存为 `source_id = 'IFS'` 时，小写路径段 `ifs` 得 200；同周期 `gfs` 无 run 故 424。

每条用例用 per-test throwaway database，未触碰任何活库。

## 2. 实机瓦片 receipt（z4 / x12 / y6，覆盖中国主体）

数据前提（实测）：`hydro.hydro_run.source_id` 实机只有 `gfs`（小写）与 `IFS`（大写）两种拼写；周期 `2026-09-03T00:00:00Z` 上 gfs 与 ifs **各 38/38** 河网 display-ready，`2026-09-03T03:00:00Z` 零 run。

| 用例 | HTTP | 字节 | 秒 | cache | cache key 前 16 位 |
|---|---|---|---|---|---|
| 新路由 gfs 冷 | 200 | 1302580 | 2.566 | miss | `57b487e5a9d5403f` |
| 新路由 gfs 热 | 200 | 1302580 | 0.038 | hit | `57b487e5a9d5403f` |
| 新路由 ifs 冷（同 cycle/valid_time） | 200 | 1302548 | 1.728 | miss | `6c41039807ddcf7f` |
| 新路由 ifs 热 | 200 | 1302548 | 0.033 | hit | `6c41039807ddcf7f` |
| 旧 5 段路由 冷 | 200 | 1302548 | 1.685 | miss | `aba7d520afbaae84` |
| 旧 5 段路由 热 | 200 | 1302548 | 0.052 | hit | `aba7d520afbaae84` |

- **两源确实分流**：同一 cycle/valid_time/z/x/y 下 gfs 与 ifs 的字节数、ETag、cache key 三者全部不同。ETag 之所以不同是因为字节不同（`stable_etag` 只哈希瓦片字节），不是因为身份进了 ETag。
- **旧路由行为不变**：旧路由的字节与 ETag 与 **ifs** 那张完全一致（`m16-c5573bf…`）。旧路由按 `cycle_time DESC, run_id DESC` 取每个河网的最新 run，在该周期上选中的就是 IFS 一侧——这正是改前的混源行为，说明 NULL 绑定确实让旧路由的选 run 未被收窄。它的 cache key 与两条新路由都不同（`-v5` 版本轮换后的新 key，属预期的一次性缓存失效）。

### fail-closed（周期无 run）

| 用例 | HTTP | error.code |
|---|---|---|
| `gfs` @ `2026-09-03T03:00:00Z` | 424 | `MVT_LIVE_POSTGIS_UNAVAILABLE` |
| `ifs` @ `2026-09-03T03:00:00Z` | 424 | `MVT_LIVE_POSTGIS_UNAVAILABLE` |

**偏差说明**：Epic 验收项写「某源该周期无 run 时 424」。实机上每个周期 gfs 与 ifs 都是 38/38 完整，**不存在**一源有、另一源无的天然周期，因此实机 424 用的是零 run 的周期。「一源有、另一源无」这一条由第 1 节的 throwaway-DB 集成用例确定性覆盖（可按需复现），不靠生产数据碰运气。

### 校验 422（未执行任何 SQL）

| 用例 | HTTP | error.code |
|---|---|---|
| `source=ERA5` | 422 | `VALIDATION_ERROR`（`path.source`） |
| `source=best` | 422 | `VALIDATION_ERROR`（`path.source`） |
| `cycle=not-an-instant` | 422 | `VALIDATION_ERROR`（`path.cycle`） |
| `cycle=2026-09-03T00:00:00.500Z` | 422 | `VALIDATION_ERROR`（秒精度） |

四条均在 4 ms 量级返回，对比冷瓦片 1.7–2.6 s，佐证校验早于任何 SQL。

### 时间实例拼写归一

| 拼写 | HTTP | cache | cache key 前 16 位 |
|---|---|---|---|
| `2026-09-03T00:00:00Z` | 200 | miss→hit | `57b487e5a9d5403f` |
| `2026-09-03T00:00:00.000Z` | 200 | hit | `57b487e5a9d5403f` |
| `2026-09-03T00:00:00+00:00` | 200 | hit | `57b487e5a9d5403f` |

三种拼写命中同一条缓存，未写出第二份瓦片。

## 3. 覆盖到的 Evidence Floor 项

group 3 Evidence Floor 中属于本 issue 的实机项全部满足：新路由 gfs/ifs 同 cycle 各一张 z4 瓦片（200、字节非空、ETag 不同）、无 run 时 424、旧路由仍 200、冷/热耗时已记录。`cycles` 端点与 57 项 valid-times 属 I5/#2008，不在本 receipt。

## 4. 清理

`/home/nwm/wt-2007` worktree、`/home/nwm/tmp/mvt-cache-2007` 缓存目录与 `/home/nwm/run-2007-*.{sh,log}` 在合并后移除；生产服务与 `/home/nwm/NWM` 工作树自始至终停留在 master。
