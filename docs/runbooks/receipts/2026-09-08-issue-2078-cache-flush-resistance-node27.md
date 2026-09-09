# Receipt: issue #2078 display 目录缓存冲刷抵抗——node-27 修复前/后对比（2026-09-08）

对象：PR #2176 head `2adef15d`（round-1 修复后）vs master `4c79cc39`。两次都在 node-27 一次性 worktree + 隔离 uvicorn `127.0.0.1:8090 --workers 1` 上跑同一脚本（`receipt-2078.sh`），生产 :8080、活动树与生产缓存未动。
`probe_app.py` 包装模块把预热间隔压到 5 s 并在 `_replay_targets`/`_store_value` 上挂文件日志作 oracle（PG 采样在 uvicorn 进程里看不到回放，见实测 receipt §4）。DB 是生产 PG :55432 只读角色，`hydro_run` `total_count=7227`。不含 DSN / token。
前置实测（冷 miss 量纲、冲刷与自伤复现）见 `2026-09-08-issue-2078-cache-flush-measurement-node27.md`。修复后一半先在 `d0268f84` 跑过一次（数字同量级），本 receipt 记录的是 `2adef15d` 的重测。

## 0. worktree 内 pytest（fixture 4.2 首句）

`2adef15d` worktree 自建 venv（Python 3.11.15），fixture 4.1 的 10 个 suite：
`test_display_catalog_cache` / `test_hydro_display_mvt_scaling` / `test_precip_overlay` / `test_api_contract` / `test_forecast_api` /
`test_openapi_drift` / `test_openapi_31_contract` / `test_openapi_response_conformance` / `test_select_ci_tests` / `test_node27_mvt_prewarm` →
**1210 passed in 257.77s**。

## 1. 冲刷（fixture 4.2 (a)）：255 条谓词为假的 key（`basin_id=junk-<i>&limit=1`）

| 步骤 | 修复前 `4c79cc39` | 修复后 `2adef15d` |
|---|---|---|
| A0 合法 `/api/v1/runs` 预热后 3 样本 | 3.3 / 3.3 / 3.3 ms | 3.2 / 3.8 / 3.4 ms |
| A1 注入 255 条（串行） | 10.26 s | 10.90 s |
| A2 注入后合法 key 3 样本 | **83.6** / 3.7 / 3.3 ms（整表清空，冷） | **4.9 / 3.1 / 2.4 ms**（命中） |

## 2. LRU 诚实边界（4.2 (a2)）：可缓存**非空**分页 `offset=<k>&limit=<l>`

| 步骤 | 修复前 | 修复后 |
|---|---|---|
| 两批各 150 条，两批之间合法 key | 3.6 ms | 4.5 ms |
| 第二批之后合法 key 3 样本 | **78.7** / 3.6 / 3.3 ms（`clear()` 再次落下） | **7.9 / 3.6 / 3.6 ms**（命中把它移到 LRU 尾部，第二批 150 条挤不到它） |
| 变体二：先命中 `discharge/cycles?source=gfs`，再**单批 300 条**不碰合法/cycles key，之后合法 key | （修复前未跑此段） | 4.3 / 3.5 / 3.5 ms |
| 同上，cycles key | — | 4.2 / 2.9 / 2.9 ms |

变体二在这套探针里连一次冷 miss 都没出现：探针把预热间隔压到 5 s，300 条串行注入约 28 s，期间预热线程按命中计数把合法 key 与 cycles key 排在前 32 条、每 5 s 回放一次（force-refresh 写回 `_store` 即回到 MRU 端），所以它们从未被挤到 LRU 头部。生产 45 s 间隔下，一次 45 s 内塞满 256 条可缓存 key 的突发**会**让合法 key 冷一次（design D1 的诚实边界），但不会整表归零——修复前那一栏的 78.7 ms 就是整表归零的样子。

## 3. 回放上界（4.2 (b)）：`probe_app.py` 钩子，间隔 5 s，观察 ≥ 3 个 tick

| tick | 修复前 `replay start` | 修复后 `replay start` |
|---|---|---|
| 1–2（注入前/中） | `n=112 junk=110` | `n=2 junk=0` |
| 3 起 | `n=37 junk=1`、`n=124 junk=1`、`n=45 / 88 / 88 junk=0` | 连续 12 个 tick **`n=32 junk=0`**，首条在 `/api/v1/runs`、`/api/v1/runs?offset=0&limit=50`（与 `/api/v1/runs` 同 key：默认 `limit=50`，见下）、`/api/v1/layers/discharge/cycles?source=gfs` 之间轮换 |
| `_store_value` 总次数（约 100 s 观察窗） | 1041（约 60 s） | 924（约 100 s，全部是可缓存的非空分页，每 tick ≤ 32） |

修复后每 tick 恰好 32 条、`junk=0`（谓词为假的 255 条一条都没成为回放目标）。首条偶尔显示为 `offset=0&limit=50`：那与不带参数的 `/api/v1/runs` 是**同一个** key（`runs:None:None:None:None:50:0`，默认 `limit=50`），分页突发里的 `i=49` 以另一种拼写命中了它、命中计数续上而 path 被改写。
下一次不带参数的命中又把 path 写回 `/api/v1/runs`；两种拼写的 loader 输入相同，回放结果逐字节一致。修复前的 `n` 由期间的 `clear()` 截断（否则是整个热集合，独立进程实测 254 条 9.2 s/tick）。

## 4. 并发与契约（4.2 (c)(d)）

| 项 | 修复前 | 修复后 |
|---|---|---|
| C 20 并发 × 40 条谓词为假 key | 40 × 200 | 40 × 200，无 500 |
| D `/api/v1/layers?offset=999999999` / `offset=0&limit=1` | 500 / 500 | 500 / 500 |
| F ≥ 3 个 tick 之后合法 key | 14.9 / 4.0 / 3.2 ms | 5.4 / 5.2 / 2.8 ms |
| uvicorn 500 | 仅 D 的两条 `/api/v1/layers` | 同左 |

D 的 500 与本 change 无关：生产 DB 缺 000057 迁移（`rnv.geometry_generation does not exist`，#2145），master 与本 PR 的 `/api/v1/layers` 都 500；`offset` 契约由 `tests/test_hydro_display_mvt_scaling.py::test_layers_pagination_is_applied_after_the_cache` 钉，迁移落地后可在隔离实例复核。

## 5. 结论

- 冲刷不再发生：谓词为假的 255 条与可缓存的 300 条突发（两种投放方式）都没能把合法 key 打回冷路径。
- 回放上界成立：每 tick 32 条、垃圾 0 条、合法 key 按命中计数排前；`_store_value` 的写入全部来自可缓存的非空页，且每 tick ≤ 32。
- 本 receipt 之后若 PR 还有改动，只允许是不经过上述任一测量段的代码（见 PR 偏离记录）；`apps/api/display_cache.py` 自 `d0268f84` 起未再变。
- 部署仍 deferred 到 #2162 维护窗口（活动树在 `hotfix/node27-rollback-pre-2073`）。
