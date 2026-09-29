# node-27 live browser receipts（PR #2693，master `c424f8eb2`）

部署方式：在 node-27 上 `git pull --ff-only`，执行 `vite build --outDir /home/nwm/tmp/dist-new`，然后原子替换 `apps/frontend/dist`（旧版本保留在 `/home/nwm/tmp/dist-prev`）。公网 bundle 为 `index-Cz0xGMGL.js`。浏览器为 node-27 本机的 Playwright chromium，访问 `https://test.nwm.ac.cn`，全程只读。

冷生成 gate 复现：`display.env` 中的 `NHMS_DISPLAY_MVT_COLD_LIMIT` 临时由 `8` 改为 `1`，经 `scripts/ops/start-display-api.sh` 重启后运行脚本，结束后立即恢复为 `8` 并再次重启。恢复后的 `display.env` 与备份 `cmp` 一致。两次各持续约 3 分钟，重启输出均为 `smoke check passed`。

## #2537 重试（`retry-receipt.txt`、`retry-tiles.json`，由 `fe-retry-receipt.mjs` 生成）

- 全国视图，不缩放，时间轴走 4 步，让可见瓦片全部变冷。
- 1 个瓦片返回 `503`，响应体为 `MVT_COLD_GENERATION_BUSY`，带 `Retry-After: 1`。同一 URL 的请求序列为 `[503, 200]`，从 503 到 200 用时 1927 ms。
- 页面主线程的 `performance` resource 条目中有 31 条 hydro 瓦片，与请求总数 31 一致。说明瓦片确实经 `nhms-mvt` 协议在主线程取回，而不是走 worker 内置的 fetch。
- 没有出现 mapSourceError 横幅，也没有非资源类的控制台错误（没有 AJAXError）。
- `receipt.txt` 里快速缩放阶段的 4 个 z3 `503` 没有后续重试。原因是缩放到 z7 以上后，这些瓦片被 MapLibre 放弃（abort），重试随之取消，这正是设计要求的行为。

## #2628 悬停（`receipt.txt`、`03-hover.png`、`04-selected-over-hover.png`，由 `fe-live-receipt.mjs` 生成）

- 用真实指针扫过地图网格。命中 `basins_shj_shud_shud_riv_001394` 后，`data-hovered-segment-id` 等于该河段 id；在同一河段内抖动，id 保持不变；移到空白处后清空。
- 点击后 `data-selected-segment-id` 等于同一 id，河段曲线窗正常打开。截图中先是青色悬停，点击后变为橙色选中，橙色压在青色之上。
- style 栈序（从 react-map-gl 的 map 实例只读取得）：`…discharge-line-hit → hover-halo → hover-line → selected-halo → selected-line`。
- 悬停扫描期间记录到 longtask 12 个。
- 与 yd 的并排观感对照没有做，只核对了样式参数（与 yd 相同：白 8 px/0.55，青 `#22d3ee` 4.5 px）。

## #2650 代站层序：线上 oracle-blocked

- 生产环境中 `GET /api/v1/met/stations?...basin_version_id=basins_hlj_vbasins` 稳定返回 500 `DATABASE_ERROR`（自 2026-09-23 起，公网请求同样失败）。全国视图下代站要素数一直是 0，所以线上拿不到「代站压在河段之上」的画面。该问题不在本批范围，已另立 #2694：`met.interp_weight`、`met.met_station` 统计陈旧，导致 Nested Loop 退化到 83.9 s，超过只读角色的 30 s statement_timeout；前端在任一流域失败时会清空全部代站。
- 走 3 步时间轴，每一步 discharge overlay 都重新挂载，栈序中各层完整，没有出现错误横幅。代站层的顺序由单测中的有状态 map stub 保证（`m11MapPrimitives.test.ts`）。
