# Receipt: issue #2078 display 目录缓存冲刷与预热回放实测（node-27，2026-09-08，修复前）

对象：`apps/api/display_cache.py` 的进程内目录缓存（`_MAX_ENTRIES = 256`，到顶整表 `clear()`，热 path 在查找前登记，预热线程每 45 s 回放活跃窗口内全部热 path）。目的：把 issue 里"12s 级"历史数字换成今天的冷 miss 量纲，并实测"256 个垃圾 key 冲刷 + 预热自伤"是否成立。所有请求都是只读 GET；不含 DSN / token。

## 0. 环境

| 项 | 值 |
|---|---|
| 生产 display API | node-27 `127.0.0.1:8080`，活动树 `5a86841c`（`hotfix/node27-rollback-pre-2073`，#2079 未部署 → 字面 `x-nhms-cache-warm: refresh` 仍被尊重，因此冷路径可用只读 GET 量到） |
| 隔离实例 | 一次性 worktree `/home/nwm/tmp/wt-2078*` @ master `4c79cc39`，`uvicorn --workers 1`（一个进程 = 一份 store = 一条预热线程，可归因），`127.0.0.1:8090`，`application_name=nhms-2078*-measure`，随机 token，空的 `NHMS_MVT_FILE_CACHE_DIR`；读的是生产 PG :55432（只读角色）；跑完 `git worktree remove --force` 并确认 `:8090` 关闭 |
| 采样 | `curl -w '%{http_code} %{time_starttransfer}'`（TTFB，秒）；PG 侧 `pg_stat_activity` 按 `application_name` 过滤，0.2 s 采样 `state='active'` 计数与 3 s 内的 `query_start` |
| 脚本 | 本地 scratch `measure-2078.sh`（256 key）/ `measure-2078b.sh`（255）/ `measure-2078c.sh`（253），投送到 `/home/nwm/tmp/`，`setsid nohup` 分离 |

生产 `--workers 2`：每个 worker 各一份缓存与预热线程；本 receipt 的隔离实例是单 worker，生产上的放大系数 ×2、冲刷需分别命中两个 worker。

## 1. 实测 1：生产冷 miss 量纲（:8080，2026-09-08T18:25Z）

方法：每端点先 3 次不带头（命中基线），再 10 次带 `x-nhms-cache-warm: refresh`（生产代码仍把它当强制刷新 → 每次真跑 loader）。

| 端点 | 命中（不带头，3 样本） | 冷（refresh，10 样本） | 冷 p50 / p95 |
|---|---|---|---|
| `/api/v1/layers` | 101.1 / 3.5 / 3.3 ms（首样本是 60 s TTL 过期后的 stale 重算） | 69.4, 81.5, 97.6, 76.2, 85.2, 94.0, 95.2, 82.5, 95.6, 96.8 ms | **85.2 / 97.6 ms** |
| `/api/v1/runs` | 84.3 / 76.9 / 2.5 ms（前两样本同上） | 80.3, 81.4, 84.1, 81.7, 82.5, 83.2, 87.6, 86.6, 87.3, 87.4 ms | **83.2 / 87.6 ms** |

结论：今天一次冷 miss ≈ 80–100 ms，命中 ≈ 2–4 ms（≈ 30×）。issue 引用的"12s 级"是历史读数，本 change 的定量以 85 ms 为准。

## 2. 实测 2：256 个垃圾 key 冲刷（隔离实例，master，18:26Z）

| 步骤 | 结果 |
|---|---|
| T0 预热 `/api/v1/runs` 后 3 样本 | 2.3 / 2.0 / 2.0 ms |
| T1 探针 `/api/v1/layers?offset=999999999` | **500**，72 ms（master 代码对生产 DB 缺 000057 迁移 → #2145；不是 offset 的锅，生产 :8080 上同一探针是 200） |
| T1 探针 `/api/v1/runs?basin_id=nhms-cache-probe-zzz&limit=1` | 200，38.8 ms（空页，可缓存） |
| T2 串行注入 256 个 `/api/v1/runs?basin_id=junk-<i>&limit=1` | 墙钟 10.98 s（≈ 43 ms/条，每条两条 SQL） |
| T3 注入后立刻 `/api/v1/runs` 3 样本 | **71.3** / 3.5 / 3.2 ms —— 刚被命中的合法 key 回到冷路径：`_store` 被整表 `clear()`，冲刷复现 |
| T4 150 s 内预热线程回放（PG 采样） | active 样本 2 / 750，distinct `query_start` 1 —— **没有**回放风暴 |
| T5/T6 之后的 `/api/v1/runs` | 5.7 / 3.7 / 3.6 ms；静置 100 s 后 4.6 / 3.5 / 3.5 ms |
| uvicorn 500 | 仅 T1 的 `/api/v1/layers?offset=999999999` 一条 |

T4 没观测到自伤的原因是代码形状本身：恰好到顶时 `_hot_paths` 与 `_store` 被**同一轮**注入清空（`_record_hot_path` `:113-114`），预热线程手里只剩清空之后登记的 1–2 条 path。也就是说"256 个 key"是冲刷的最小剂量，但**不是**自伤的剂量——自伤需要一次 ≤ 255 条（含探针与合法 key 共 ≤ 255 条目）的突发，让 `_hot_paths` 满而不清。

## 3. 实测 2b：255 个垃圾 key（18:34Z）

与实测 2 同形，注入 255 条。T3 首样本 **79.5 ms**（再次冲刷）、T4 active 0 / 750。原因同上：合法 key + T1 探针 + 255 = 257 条，第 255 条注入时仍到顶清空。这一轮只额外证明"冲刷阈值 = 256 条**条目**，与谁先占位无关"。

## 4. 实测 2c：253 个垃圾 key（不触发 clear，18:41Z）

合法 key + T1 探针 + 253 = 255 条 < 256，`_store` 与 `_hot_paths` 都满而不清：

| 步骤 | 结果 |
|---|---|
| T0 `/api/v1/runs` 3 样本 | 3.8 / 3.3 / 3.3 ms |
| T2 注入 253 条 | 墙钟 10.68 s（≈ 42 ms/条） |
| T3 注入后 `/api/v1/runs` 3 样本 | **3.8 / 3.3 / 3.2 ms** —— 命中，没被清（与 2/2b 的 71–80 ms 对照，冲刷阈值就是第 256 条条目） |
| T4 200 s（≥ 4 个 45 s tick）PG 采样 | active 0 / 1000，distinct `query_start` 0 |
| T5/T6 | 4.5 / 3.1 / 2.2 ms；静置 100 s 后 5.0 / 3.6 / 3.5 ms |

T4 仍然是空的：PG 侧 0.2 s 采样在 4 个 tick 内没看到任何活动，尽管 `_hot_paths` 里有 254 条活跃 path。§5/§6 证明这是**采样 oracle 的问题**而不是回放没发生：同一采样器在 §5 的独立进程里能看到回放（31/122 个 active 样本），在 uvicorn 进程里却看不到，原因未查明（本 receipt 不再依赖它；修复后对比用 §6 的进程内钩子当 oracle）。

## 5. 实测 d：一个 tick 回放 254 条热 path 的直接代价（独立进程，18:53Z）

不经 uvicorn：在一次性 worktree 里以 `.venv/bin/python` 导入 `apps.api.main:app`（真实 app + 真实 DB，`application_name=nhms-2078d-measure`），先停掉导入时起的预热线程。
手工把 253 条 `/api/v1/runs?basin_id=junk-<i>&limit=1` + 1 条 `/api/v1/runs` 写进 `_hot_paths`，包一层计数在 `_store_value` 上，直接 `asyncio.run(_replay_targets(app, targets))`；然后把 `DISPLAY_CATALOG_WARM_INTERVAL_SECONDS` 压到 1 s 起线程跑 20 s。

| 测量 | 结果 |
|---|---|
| 手工回放第 1 轮 | 254 条，墙钟 **9.16 s**，`_store_value` 254 次，**36.1 ms/条** |
| 手工回放第 2 轮 | 254 条，墙钟 9.35 s，254 次，36.8 ms/条 |
| 线程版 `_warm_loop`（间隔 1 s）20 s | `_store_value` 495 次 ≈ 1.9 个 tick（每 tick ≈ 9.2 s，与手工一致）；`stop_display_catalog_warmer` 正常返回 |
| PG 采样（同一采样器） | active 31 / 122 样本，distinct `query_start` 43，busy 秒连续成簇 |

## 6. 实测 e：uvicorn 进程内的预热线程到底做了什么（18:56Z）

用一个 `probe_app.py` 包装模块（先 `import apps.api.display_cache`，把间隔改成 5 s，在 `_warm_loop` / `_replay_targets` / `_store_value` 上挂文件日志，再 `from apps.api.main import app`），`uvicorn probe_app:app --workers 1 :8090`；预热 `/api/v1/runs` 3 次后注入 100 条垃圾 key，静置 30 s。

| 时刻（本机时区） | 事件 |
|---|---|
| 02:56:14 | `warm loop start`（线程名 `display-catalog-warmer`，在 app 导入时就起） |
| 02:56:19 → :23 | `replay start n=95` → `replay end`（注入还在进行，95 条已登记） |
| 02:56:28 → :32 | `replay start n=101` → end（100 垃圾 + 1 合法，**每条都回放**） |
| 02:56:37 → :40、02:56:45 → :49 | 同上，每 tick 101 条 ≈ 3.5–4 s ≈ 38 ms/条 |
| `_store_value` 每秒计数 | 回放期间 22–30 次/s；无异常、无 500 |

## 7. 结论（喂给 fixture 的数字）

1. **冷 miss 量纲**：85 ms（layers p50）/ 83 ms（runs p50），不是 12 s。
2. **冲刷**：第 256 条**条目**（含探针与合法 key）触发整表 `clear()`，刚被命中的合法 key 立刻回到 71–80 ms；`_hot_paths` 同一轮被清。
3. **预热自伤成立**：差一条到顶的突发（本例 254 条）让预热线程**每 45 s 串行回放全部** 254 条，每 tick 9.2 s（36–38 ms/条），持续到活跃窗口 1800 s 过期——一次 11 s 的注入换来 30 min 内约 33 个 tick（周期 = 45 s 等待 + 9.2 s 回放）× 9.2 s ≈ 5 min 的 DB 串行工作；生产 `--workers 2` 时按 worker 各算一份。
4. 修复后 receipt 的 oracle：TTFB（冲刷）+ 进程内钩子的每 tick `_store_value` 次数（回放上界），不用 PG 采样。
5. 顺带：master 代码的 `/api/v1/layers` 对生产 DB 500（缺 000057 迁移，#2145），本 change 的 `offset` 契约用本地路由用例钉，隔离实例只能在迁移落地后复核。
