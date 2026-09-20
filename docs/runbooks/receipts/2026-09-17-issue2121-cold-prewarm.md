# #2121-B 后真实冷启 prewarm receipt — node-27, 2026-09-17

## 顺序与代码身份

\#2121-B 先由 PR #2449 合并，merge SHA `d3d09d9be9f52f02de45cf1727bbe5d2f8077feb`；本次在该已合并代码的 node-27 隔离 checkout `/home/nwm/tmp/nwm-2121-b` 执行。B 新增逐源 discharge_ok/failed，summary 为 v3。此 receipt 产出前没有修改 #2121-A / #2346 第二步的旋钮。

测量窗口 UTC 03:27–03:29（node-27 本地 11:27–11:29）。生产 API `:8080` 保持原 SHA `7ecc46bed18cc8b6f20030f49aa3bf43a77b6858`、MainPID `3023964`；被测 API 是相同入口/数据库/角色/环境的独立双 worker 实例 `:8092`。两树的 display API 差异仅是 master 已合并的无 hydrological run 时保留 precip layer 定义；被测瓦片路径未变。未部署新的生产 API。

## 冷态、容量和安全边界

- 使用生产 `infra/env/display.env`，仅覆盖被测进程的 `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/tmp/issue2121-cold-20260917/cache`；目录初始 **0 文件**，完成后 183 文件。没有清空/改写生产缓存。
- DB `map.tile_cache` 测前/后 **0 行**，显示角色 `nhms_display_ro` 无 INSERT 权限。逐请求原始样本复算：**183 个 HTTP 200，183 个 X-Tile-Cache=miss**，不是仅以重启或 cache_hits=0 推断冷态。
- 这是应用瓦片文件缓存冷，不是 PostgreSQL shared_buffers/OS page cache 冷；不人为 flush DB/OS cache。
- 两实例各 2 worker、每 worker pool_size=8 / max_overflow=8（沿用 #2441）。`max_connections=100`、reserved=3；其它活动实测 11，按至少 16 预留，加显式 headroom 8：`2 × 2 × (8+8) + 16 + 3 + 8 = 91 ≤ 100`。
- `statement_timeout=0`、`max_parallel_workers_per_gather=20`，本次未改。独立实例没有真实用户与 prewarm 争同一池，因此本 receipt **不证明** #2346 的冷突发隔离已解决。
- 只临时暂停原本 active 的 autopipe timer，确认 service inactive；设置 15 分钟自动恢复保护。冷 API 使用独立 transient unit，RuntimeMaxSec=900 / TimeoutStopSec=15。测后已停止冷实例、恢复 autopipe timer active、取消恢复保护 timer。生产 API MainPID 始终 3023964。
- 磁盘：`/` 18G 可用，`/home` 1.1T 可用，`/data/GHDC` 12T 可用；TMPDIR 为 `/home/nwm/tmp`。
- 测量结束附近的观测：生产 health/layers 均 200，分别约 5.6/4.4 ms；当时 display idle connections=30。不是峰值或全程连续采样。停止冷实例后回到 16，health/layers 约 4.4/3.3 ms，仍为 200。

## 同次运行方法

使用 cron 的 `infra/env/node27-ingest.env`（包含有效 warm token，不记录秘密），沿用 `AUTOPIPE_MVT_PREWARM_WORKERS`/zooms 口径。throwaway harness 直接调用已合并脚本 `prewarm()`，仅通过已有 `warm=` 参数在真实 `warm_url()` 周围测量 monotonic 耗时；没有替换 scheduler、discovery 或 HTTP transport。HTTP/transport 失败也保留耗时；summary 与每请求样本来自**同一遍**执行。

生效参数：workers=8、socket timeout=30s、deadline=540s、lead window=12h、river zooms=3/4/5、discharge zooms=3/4。早期 issue 的 300s deadline 已不是当前代码值。

## 结果与完整 summary

运行 rc=0，墙钟 **18.771s**；183 requests，failed_count=0，deadline_skipped=0，cache_hits=0。两源同为 `2026-09-16T00:00:00Z`；各发布 56 个时次，按现行 12h 窗口各预热 5 个（仍不是全时间轴）。

| 分组 | n | p95 秒 | max 秒 | 失败 |
|---|---:|---:|---:|---:|
| 全部 | 183 | 2.494663 | 3.124972 | 0 |
| GFS discharge | 65 | 2.702293 | 2.823929 | 0 |
| IFS discharge | 65 | 2.494663 | 2.693489 | 0 |
| river | 43 | 1.693677 | 3.124972 | 0 |
| PNG | 10 | 0.995377 | 0.995377 | 0 |

p95 采用 nearest-rank `ceil(0.95*n)`，包含失败样本（本次为 0）；不是插值 percentile。183 条原始样本已独立复算状态、cache miss、max/p95 与请求计数。

```json
{
  "schema": "nhms.node27-mvt-prewarm.v3",
  "base_url": "http://127.0.0.1:8092",
  "zooms": [
    3,
    4,
    5
  ],
  "discharge_zooms": [
    3,
    4
  ],
  "workers": 8,
  "river_tile_count": 43,
  "requests_total": 183,
  "failed_count": 0,
  "cache_hits": 0,
  "bytes": 49999414,
  "failures": [],
  "elapsed_seconds": 18.771,
  "deadline_seconds": 540.0,
  "deadline_skipped": 0,
  "lead_hours": 12,
  "per_source": {
    "gfs": {
      "cycle": "2026-09-16T00:00:00Z",
      "valid_times_available": 56,
      "valid_times_warmed": 5,
      "discharge_requests": 65,
      "discharge_ok": 65,
      "discharge_failed": 0,
      "png_ok": 5,
      "png_not_mirrored": 0,
      "png_window_incomplete": 0,
      "png_out_of_contract": 0,
      "png_failed": 0,
      "error": null
    },
    "ifs": {
      "cycle": "2026-09-16T00:00:00Z",
      "valid_times_available": 56,
      "valid_times_warmed": 5,
      "discharge_requests": 65,
      "discharge_ok": 65,
      "discharge_failed": 0,
      "png_ok": 5,
      "png_not_mirrored": 0,
      "png_window_incomplete": 0,
      "png_out_of_contract": 0,
      "png_failed": 0,
      "error": null
    }
  }
}
```

## 数据依据下的联合批次决策

\#2121-A：**保持** prewarm workers=8、timeout=30s、deadline=540s 与 display pool 8+8 / 2 workers，不据此继续扩大池或超时。最大样本 3.125s，30s 超时约为其 9.6 倍；当前包络 18.771s，距 540s 预算很远，且无失败/跳过。这个结论仅覆盖本次现行包络，不能外推真实用户争用或全时间轴成本。

\#2346 第二步仍必须做冷生成隔离；与 A 保持同一后续 PR、配置/部署/回滚批次。全时间轴扩窗、冷生成容量及角色 SQL 的最终选择须在联合批次中结合真实突发、EXPLAIN 和复测证据完成。仅增大 producer 外围 semaphore 而让等待者继续持有 digest/session 连接，不能宣称实现连接预留。

## 原始证据

node-27：`/home/nwm/tmp/issue2121-cold-20260917/cold-receipt.json`、`prewarm-summary.json`、`request-samples.json`。本地：`/tmp/nwm-2121-native/.workplans/2121/`，含 admission、恢复前后探针和完整 isolated API 日志。原始 JSON 与样本保留供复核，不删除证据目录。

- cold-receipt.json SHA256：`16bc0fce2eeb9c9fbda6475834232e34799a7ac7f71f9eba15ae9c61a37e9103`
- request-samples.json SHA256：`cbadfc3de585d177fce3e7cd45b0e10e2506c556b258d798d96c9973f6af7197`

\#2017 receipt 的 175/183 cache hits、1.891s 不能替代本次冷态证据。这里没有关闭 #2121 或 #2346，也没有声称全时间轴或实际用户突发验收通过。
