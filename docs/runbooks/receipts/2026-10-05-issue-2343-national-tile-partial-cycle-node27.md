# #2343 全国流量瓦片部分覆盖周期拒绝：node-27 live receipt

- 采集时间：2026-10-05 03:46Z，node-27 生产 display API（`127.0.0.1:8080`），checkout `330c8d03`，display 进程自 2026-10-05 02:04:51Z 起运行。
- 只做了 4 次 curl，没有执行 SQL。

## 结果

| 请求 | 状态 | 字节 | 耗时 | sha256 前 16 位 | `x-tile-cache` |
|---|---|---|---|---|---|
| `/api/v1/tiles/hydro-national/gfs/2026-10-04T00:00:00Z/q_down/2026-10-04T00:00:00Z/5/26/12.pbf`（已列出周期，第 1 次） | 200 | 1359600 | 25.13 s | `18bd147768ff8a31` | miss |
| 同上（第 2 次） | 200 | 1359600 | 0.038 s | `18bd147768ff8a31` | hit |
| `/api/v1/tiles/hydro-national/gfs/2026-08-25T00:00:00Z/q_down/2026-08-25T03:00:00Z/5/26/12.pbf`（目录未列出的周期） | 424 | - | 0.156 s | - | - |

424 的响应体：`code=MVT_NATIONAL_IDENTITY_INCOMPLETE`，`details={"layer_id":"discharge","source":"gfs","cycle":"2026-08-25T00:00:00Z","covered_network_count":21,"active_network_count":66}`。

`/api/v1/layers/discharge/cycles?source=gfs` 当时列出 12 个周期，`2026-09-28T12:00:00Z` 至 `2026-10-04T00:00:00Z`。

## 没有取得的验收项

- **部署前后对照**（完全覆盖周期的字节与 sha256 不变、缓存未命中延迟的前后对比）：含 #2153 的 build 早已上线，部署前的样本无法再取。本 receipt 只有当前状态。
- **`covered_network_count` / `active_network_count` 与 SQL 实测一致**：没有执行对照 SQL，21 / 66 是接口自己报的数。

## 备注

已列出周期的 z5 全国瓦片冷生成用了 25 秒，接近 display 角色 30 秒的 statement timeout。
