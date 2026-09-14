# node-27 前端部署后浏览器 receipt：渲染异常收敛到区域边界（#2347 E8）

- 日期：2026-09-14，采集于 06:28 UTC
- 入口：`https://test.nwm.ac.cn/`（公网，只读浏览器会话）
- 关联：#2347（拆自 #2129 裁决 C），PR #2351

## 部署（仅前端，owner 2026-09-14 授权，方法同 #2336）

| 项 | 值 |
|---|---|
| 构建提交 | `fcbe01622`（PR #2351 head，基于 master `c6895bea5`） |
| 构建位置 | node-27 临时 worktree `/home/nwm/NWM-fe-2347`（detached），`corepack pnpm install --frozen-lockfile && corepack pnpm build`，19.2 s；替换后已 `git worktree remove` |
| 替换方式 | `cp -a` 到 `apps/frontend/dist.new-2347`，旧 `dist` `mv` 为备份，新目录 `mv` 为 `dist`；静态文件每次请求读盘，无需重启 API |
| 线上 bundle | 替换前 `index-CYsR5Fwk.js`（#2336 部署），替换后 `index-DYYQu9Ic.js`（`curl` 公网首页确认） |
| 回滚 | 旧 dist 备份在 `/home/nwm/fe-dist-backups/dist.bak-20260914-issue2347`；回滚 = `mv` 回 `apps/frontend/dist` |
| 未动 | 主 checkout（`a8db554d`）、API 进程、数据库、迁移均未动；#2336 receipt 记录的前端/API 版本漂移仍然存在 |

## 采集方式

- 本地 Playwright chromium（1440×900）打开线上默认页 `/`，脚本 `capture.cjs`，原始输出 `capture.log`。
- A：不做任何修改，检查页面上没有任何 `region-error-*` / `route-error-fallback`。
- B：只在浏览器端注入，服务端不受影响。用 `page.route` 取回真实的 `/api/v1/layers/discharge/cycles?source=gfs` 响应，把 `cycles` 改成 `[null, …原数组]`，`default_cycle` 保持不变。
- 截图缩到 1100 px 宽、JPEG 质量 70 入库；原尺寸 PNG 留在本地 `.workplans/issue-2347/evidence/`（gitignored）。

## 结果

| 验收项 | A 未修改 | B 注入 `null` |
|---|---|---|
| 边界 fallback | 无 | 只有 `region-error-control-bar`（「此区域加载失败」+「重试」） |
| 顶栏 `header` | 在 | 在 |
| `m11-fullscreen-map` 与地图 canvas | 在 | 在 |
| 底部控制条 `m11-bottom-control-bar` | 在 | 不在（由 fallback 替代） |
| `hydro-national` 流量瓦片 | 6/6 为 200 | 6/6 为 200 |
| `[RegionErrorBoundary]` console.error | 0 | 1 |

- 截图：`a-unmodified-default.jpg`、`b-injected-null-cycle.jpg`。
- B 截图里图层开关、底图开关、图例和河网流量都正常显示，底部居中是控制条的 fallback。
- 改动前，同样的 payload 会让控制条派生在 render 中抛 `TypeError`，整棵 React 树卸载，页面空白（单测 E4 的改动前红记录：`.workplans/issue-2347/evidence/e4-red.txt`）。

**判定：E8 PASS。**
