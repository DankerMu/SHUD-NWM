# #2550 天地图同源代理：node-27 loopback smoke receipt（2026-09-22）

- 执行位置：node-27，`nwm` 用户；PR #2551 分支在隔离 worktree `~/NWM-pr2551` 中运行（执行后已删除）。
- 服务：独立 uvicorn，`127.0.0.1:18023`，1 个 worker；环境来自 `infra/env/display.env`，
  其中 `NHMS_MVT_FILE_CACHE_DIR` 改指向 scratch 目录 `~/tmp/pr2551-smoke/cache`。
- 未触碰：生产树 `/home/nwm/NWM`、`:8080` 上的 display 服务、nginx、生产缓存目录。
- 复现脚本：[`smoke.sh`](smoke.sh)。脚本不输出 key，输出再经 `tk=` redact。

## 结果

| 请求 | 结果 | 结论 |
|---|---|---|
| `vec/1/1/0`（第 1 次） | `503`，`Cache-Control: no-store`，`Retry-After: 60`，`BASEMAP_UPSTREAM_THROTTLED` | 新进程冷却为 0，这次确实请求了上游；天地图返回 429（key 仍在限流），被转成不可缓存的 503 |
| `vec/1/1/0`（第 2 次） | 同上 | 由冷却直接应答，未再请求上游 |
| `cva/2/1/1` | 同上 | 冷却对所有图层生效 |
| `osm/1/1/0` | `422 VALIDATION_ERROR` | 图层白名单生效，未请求上游 |
| `vec/1/5/0` | `404 BASEMAP_TILE_OUT_OF_RANGE` | 越界检查生效，未请求上游 |
| 缓存目录 | 0 个文件 | 失败响应从不缓存 |

uvicorn 日志中只有上面 5 条 `api_error` 记录，没有 traceback。

## 未覆盖（key 限流期间无法取证）

- 200 + 写入缓存 + 第二次命中 `X-Tile-Cache: hit` 的 live 路径（单测 `tests/test_basemap_proxy.py` 已覆盖）。
- 「浏览器 UA + 不带 Referer」在 key 未限流时是否返回 200：依据是 2026-09-20 receipt §2.1 B1。
  限流期间天地图对任何形态的请求都返回 `302010`，所以本次无法独立复核。
- Tianditu `_w` 图层是否提供 `l=0`：未知。若不提供，完全缩小时 z0 会触发 502 和底图提示。
  key 恢复后应复核，必要时给 raster source 加 `minzoom: 1`。
