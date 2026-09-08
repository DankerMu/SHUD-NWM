# Issue #2032 — MVT 瓦片缓存容量与冷耗时实测（node-27，2026-09-08）

本 receipt 是 issue #2032 验收标准第 1 条（node-27 实测）与第 2 条（裁定生产在涨的是 DB 侧还是文件侧）的证据。
所有数字均为实测，取数时间 2026-09-08 18:50–19:30 CST。

## 0. 取数环境

| 项 | 值 |
|---|---|
| node-27 活动 checkout `/home/nwm/NWM` | `5a86841c`（2026-09-05），落后 `origin/master` `565ea882` 142 个提交；工作树无修改，仅 untracked 证据目录 |
| 运行中的 `nhms-display-api` | `ActiveEnterTimestamp=2026-09-06 08:34:24 CST`，即以 `5a86841c` 之前的代码运行 |
| `GET /api/v1/layers/discharge/cycles?source=gfs` 在 :8080 | **404**——#2008 的 cycles 端点尚未部署到该实例（代码在 master 上存在）；不是端点故障 |
| 文件缓存根 | `/home/nwm/.cache/nhms/mvt`（`nhms-display-api.service` ExecStart 默认值；`infra/env/display.env` 未显式设置） |
| 第二实例 `yd-display-api`（:8081，checkout `/home/nwm/yd-NWM` @ `b991b507`） | 缓存根 `/home/nwm/.cache/yd-nwm/mvt`，**独立目录**，与本 issue 的回收对象无交集 |
| 冷耗时探针 | 独立 uvicorn `127.0.0.1:8090`、`--workers 1`、空缓存目录 `/home/nwm/tmp/mvt-2032-probe`（探完删除），同一 `display.env`，不碰生产实例与生产缓存 |

## 1. issue 指定的两条 SQL

```sql
SELECT count(DISTINCT (lower(source_id), cycle_time)) FROM hydro.hydro_run
WHERE status IN ('succeeded','parsed','published');
-- 406

SELECT count(*), pg_size_pretty(pg_total_relation_size('map.tile_cache')) FROM map.tile_cache;
-- 0 | 32 kB
```

补充：

| 项 | 值 |
|---|---|
| display-ready 周期 `min/max/count(DISTINCT cycle_time)` | `2026-05-31 06:00Z` / `2026-09-07 12:00Z` / 203 |
| `map.tile_cache` 权限 | `nhms_display_ro`: SELECT only；`nhms_ingest_rw`: 全权限 |
| 每周期 display-ready 活动河网数 | 最新 6 个身份（09-06T12 … 09-07T12，gfs/ifs）均 38/38；最老身份（05-31T06 … 05-31T18）仅 2/38 |

## 2. 文件侧

| 项 | 值 |
|---|---|
| `du -sh /home/nwm/.cache/nhms/mvt` | 731M（`.locks` 1.1M，全是目录块） |
| `.pbf` 数（排除 `precip/`） | 4380 |
| `.pbf` 字节总和 | 757 397 895 |
| `.locks/**` 文件数 | 4383 |
| `*.tmp` 中间文件 | 0 |
| `precip/` 文件数 | 0（目录尚不存在） |
| 一级 hex 目录数 | 256 |

按 mtime 的日分布（`.pbf` / `.lock`）：

| 日期 | `.pbf` | `.lock` |
|---|---|---|
| 2026-09-04 | 2165 | 2168 |
| 2026-09-05 | 367 | 367 |
| 2026-09-06 | 272 | 272 |
| 2026-09-07 | 561 | 561 |
| 2026-09-08（截至 18:50） | 1015 | 1015 |

锁文件与 `.pbf` 逐日一一对应（09-04 多 3 个锁 = 3 次 424/413 判定，只留锁不留瓦片），证实：每个 miss 一个锁文件，从不回收。

## 3. 卷与 inode

```
df -h / /home
/dev/mapper/ubuntu--vg-ubuntu--lv   98G   72G   22G  78% /
/dev/mapper/ubuntu--vg-home        1.7T  1.3T  259G  84% /home
df -i / /home
/dev/mapper/ubuntu--vg-ubuntu--lv   6553600   641690   5911910  10% /
/dev/mapper/ubuntu--vg-home       110460928  2223687 108237241   3% /home
```

## 4. 裁定：生产在涨的是文件侧

- `map.tile_cache` 0 行、32 kB；display 角色对该表仅 SELECT，`_safe_write_cache` 的 INSERT 必然失败并短路到 `_safe_write_file_cache`。
- 文件侧 5 天 4380 张 / 757 MB。
- 结论：**A 方案（按 mtime 剪文件树）**是唯一剪得到增长侧的方案；B（DB 侧按身份剪）剪不到任何东西；C（sidecar）为不存在的精确性引入新状态。

## 5. 增长率与稳态投影（由第 2 节实测推算）

| 项 | 值 |
|---|---|
| 均值 `.pbf` / 天（09-04 起 5 个自然日） | ≈ 876 |
| 均值字节 / 天 | ≈ 151 MB |
| 14 天回收窗稳态（`.pbf`） | ≈ 12 300 张 / ≈ 2.1 GB |
| 14 天回收窗稳态（锁文件） | 0（锁文件改为释放即 unlink 后，只剩在途 miss 数） |
| 现存 4383 个遗留锁文件在 14 天口径下全部老化的日期 | 09-04 批次 2026-09-18 起可剪，09-08 批次 2026-09-22 全部可剪 |

增长主要由 `scripts/node27_autopipe_cron.sh` 每 tick 触发的 `node27_mvt_prewarm.py`（z3–5）驱动，不依赖用户访问。

## 6. 冷耗时对比：最新周期 vs ≥7 天前周期（z4/12/6，q_down）

TimescaleDB chunk 状态（`river_timeseries`，按 `valid_time` 分 7 天 chunk，压缩滞后 `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=172800`）：

| chunk | 范围 | 已压缩 |
|---|---|---|
| `_hyper_3_62_chunk` | 08-20 … 08-27 | 是 |
| `_hyper_3_91_chunk` | 08-27 … 09-03 | 是 |
| `_hyper_3_107_chunk` | 09-03 … 09-10 | 否 |
| `_hyper_3_110_chunk` | 09-10 … 09-17 | 否 |

探针（每行一次独立请求；`cold` = `X-Tile-Cache: miss`，`hot` = `hit`）：

| 周期 | valid_time | chunk | HTTP | 字节 | 秒 | cache |
|---|---|---|---|---|---|---|
| gfs 09-07T12（最新） | 09-08T00 | 107（未压缩） | 200 | 1 374 286 | 2.199 | miss |
| 同上 | 同上 | | 200 | 1 374 286 | 0.038 | hit |
| gfs 09-07T12 | 09-08T12 | 107 | 200 | 1 374 309 | 1.821 | miss |
| gfs 09-07T12 | 09-09T00 | 107 | 200 | 1 374 321 | 1.824 | miss |
| ifs 09-07T12 | 09-08T00 | 107 | 200 | 1 374 055 | 2.895 | miss |
| gfs 09-01T00（7 天前） | 09-01T12 | 91（**压缩**） | 200 | 1 374 252 | 2.025 | miss |
| 同上 | 同上 | | 200 | 1 374 252 | 0.035 | hit |
| gfs 09-01T00 | 09-02T00 | 91（压缩） | 200 | 1 374 240 | 1.878 | miss |
| ifs 09-01T00 | 09-01T12 | 91（压缩） | 200 | 1 373 049 | 2.053 | miss |
| gfs 08-27T12（12 天前） | 08-28T00 | 91（压缩） | 200 | 1 374 297 | 2.726 | miss |
| gfs 08-27T12 | 08-28T12 | 91（压缩） | 200 | 1 374 346 | 2.217 | miss |
| gfs 08-25T00（14 天前，**未在 cycles 交集内**） | 08-25T12 | 62（压缩） | 200 | 1 183 693 | 2.109 | miss |
| gfs 08-25T00 | 08-26T00 | 62（压缩） | 200 | 1 183 853 | 1.576 | miss |
| z3/6/3 gfs 09-01T00 | 09-01T12 | 91（压缩） | 200 | 1 846 890 | 2.677 | miss |
| z3/6/3 gfs 09-07T12 | 09-08T00 | 107（未压缩） | 200 | 1 846 919 | 2.560 | miss |

结论：压缩 chunk 内的周期冷 miss 落在 1.6–2.7 s，与未压缩最新周期的 1.8–2.9 s **同一带宽**。issue 里标注为「假设」的「老周期冷 miss 显著更贵」**不成立**。

附带事实：08-25T00 周期不在 `cycles` 交集列表里（最老的 38/38 周期是 08-27T00），瓦片路由仍返回 200，字节数 1.18 MB（少于 1.37 MB）——即**未列出的周期渲染的是部分河网的瓦片**，并被缓存。这是「入口收窄」的真正含义（语义一致性），不是成本控制。

## 7. 424 路径的真实成本

| 请求 | HTTP | 秒 |
|---|---|---|
| gfs 2026-09-07T03Z（不存在的周期）#1 | 424 | 0.038 |
| 同上 #2（重试） | 424 | 0.041 |
| gfs 2030-01-01T00Z | 424 | 0.044 |

424 一次约 40 ms（digest 查询 + 身份探针在 `source_identity_count = 0` 处短路，不建瓦片）；每个 424 仍留下一个锁文件（探针目录里 3 个 424 → 3 个孤儿 `.lock`）。issue 里「同一请求每次重试都重付全额冷成本」中的「全额」不成立：重付的是 40 ms，不是 2 s。

## 8. 对 issue 候选方案的裁定

| 候选 | 裁定 | 依据 |
|---|---|---|
| A 回收（按 mtime 剪 `<hh>/*.pbf` 与 `.locks/**`，排除 `precip/**`） | **采纳** | 第 4 节：增长全部在文件侧 |
| B DB 侧按身份剪 | 不采纳 | `map.tile_cache` 0 行、display 角色无写权限 |
| C key→身份 sidecar | 不采纳 | 引入必须与缓存一致的新状态；A 已足够 |
| Part B：锁文件释放即 unlink | **采纳** | 第 2 节：锁与 `.pbf` 一一对应、永不回收；4383 个 inode |
| Part B：424 负缓存 | 不采纳 | 第 7 节：424 仅 40 ms；TTL 负缓存会让刚发布的周期在 TTL 内继续 424 |
| 入口收窄（cycle 限于已发布列表） | 不在本 PR 采纳，另立 issue | 第 6 节附带事实：这是语义决策（未列周期是否可直连渲染部分瓦片），不是成本项；z/x/y × valid_time 才是冷 miss 空间的主轴 |
