# node-27 前端部署后浏览器 receipt：首页畸形起报时次显示「数据异常」（#2129 B E11）

- 日期：2026-09-14，采集于 07:19 UTC
- 入口：`https://test.nwm.ac.cn/`（公网，只读浏览器会话）
- 关联：#2129 裁决 B，PR #2353；前置 #2347（PR #2351）

## 部署（仅前端，owner 2026-09-14 授权，方法同 #2336 / #2347）

| 项 | 值 |
|---|---|
| 构建提交 | `bba92b7f7`（PR #2353 head，基于 master `a21f25ee1`） |
| 构建位置 | node-27 临时 worktree `/home/nwm/NWM-fe-2129b`（detached），`corepack pnpm install --frozen-lockfile && corepack pnpm build`；替换后已 `git worktree remove` |
| 替换方式 | `cp -a` 到 `apps/frontend/dist.new-2129b`，旧 `dist` `mv` 为备份，新目录 `mv` 为 `dist`；静态文件每次请求读盘，无需重启 API |
| 线上 bundle | 替换前 `index-DYYQu9Ic.js`（#2347 部署），替换后 `index-DLe5e3Ad.js`（`curl` 公网首页确认） |
| 回滚 | 旧 dist 备份在 `/home/nwm/fe-dist-backups/dist.bak-20260914-issue2129b`；回滚 = `mv` 回 `apps/frontend/dist` |
| 未动 | 主 checkout（`a8db554d`）、API 进程、数据库、迁移均未动；#2336 receipt 记录的前端/API 版本漂移仍然存在 |

## 采集方式

- 本地 Playwright chromium（1440×900）打开线上默认页 `/`，脚本 `capture.cjs`（由 #2347 的脚本改写），原始输出 `capture.log`。
- 只在浏览器端注入，服务端不受影响。用 `page.route` 取回真实的 `/api/v1/layers/discharge/cycles?source=gfs` 响应再改写，`default_cycle` 保持不变。
  - A：不做修改。
  - B：`cycles` 改为 `[null, …原数组]`。
  - C：`cycles` 改为原数组各项 `cycle_time` 组成的字符串数组。
- 截图缩到 1100 px 宽、JPEG 质量 70 入库；原尺寸 PNG 留在本地 `.workplans/issue-2129b/evidence/`（gitignored）。

## 结果

| 验收项 | A 未修改 | B `[null, …]` | C 字符串数组 |
|---|---|---|---|
| `m11-data-anomaly` | 无 | 「数据异常：起报时次返回格式不符，已按不可用处理」 | 同 B |
| 边界 fallback（`region-error-*` / `route-error-fallback`） | 无 | 无 | 无 |
| 顶栏、`m11-fullscreen-map`、地图 canvas | 在 | 在 | 在 |
| 底部控制条 `m11-bottom-control-bar` | 在 | 在 | 在 |
| `hydro-national` 流量瓦片 | 6/6 为 200 | 6/6 为 200 | 6/6 为 200 |
| `[RegionErrorBoundary]` console.error | 0 | 0 | 0 |

- 截图：`a-unmodified-default.jpg`、`b-injected-null-cycle.jpg`、`c-injected-string-cycles.jpg`。
- C 截图中控制条仍可用：起报时次回落到目录默认周期，地图上方有「数据异常」提示。
- 对照改动前：
  - B 在 #2347 部署上只显示控制条区域的「此区域加载失败」（`../2026-09-14-issue-2347-region-error-boundaries-node27/`）；
  - C 改动前没有任何提示，选择器静默只剩默认周期（单测 E7 的改动前红记录）。

**判定：E11 PASS。**
