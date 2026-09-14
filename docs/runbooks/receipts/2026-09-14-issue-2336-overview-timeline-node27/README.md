# node-27 前端部署后 C4 浏览器 receipt：时间轴逐步零请求、未列出周期文案（#2336）

- 日期：2026-09-14，采集于 03:51 UTC
- 入口：`https://test.nwm.ac.cn/`（公网，只读浏览器会话）
- 关联：#2336（承接 PR #2334 的 E12），#2127、#2131、#2139；同一份 bundle 也包含 PR #2338

## 部署（仅前端，owner 2026-09-14 授权）

| 项 | 值 |
|---|---|
| 构建提交 | `6841bd785`（origin/master，包含 `7785f23d6` PR #2334 与 `accb65819` PR #2338；与 `accb65819` 之间无 `apps/frontend` 差异） |
| 构建位置 | node-27 临时 worktree `/home/nwm/NWM-fe-2336`（detached，替换后已 `git worktree remove`），`corepack pnpm install --frozen-lockfile && corepack pnpm build`，16.88 s |
| 替换方式 | `cp -a` 到 `apps/frontend/dist.new-2336`，旧目录 `mv` 为备份，新目录 `mv` 为 `dist`；`startup_wiring.py` 的 StaticFiles/FileResponse 每次请求读盘，所以无需重启 API |
| 线上 bundle | 替换前 `index-BXgTZLaK.js`（构建于 09-04），替换后 `index-CYsR5Fwk.js` |
| 回滚 | 旧 dist 备份在 `/home/nwm/fe-dist-backups/dist.bak-20260914-issue2336`（checkout 之外，`git status` 仍干净）；回滚 = 把它 `mv` 回 `apps/frontend/dist` |
| 未动 | 主 checkout 仍停在 `a8db554d`（09-09）；API 进程、数据库、迁移 000059 均未动 |

**已知漂移：** 线上前端来自 master，而主 checkout 与 API 代码仍是 09-09 版本。

- 契约方向兼容：`a8db554d..master` 在 `openapi/nhms.v1.yaml` 与 `apps/frontend/src/api/types.ts` 上只有删除（10 行），即新前端用到的接口比旧 API 提供的少。
- 主 checkout 的分支名仍是 `hotfix/node27-rollback-pre-2073`，但 HEAD `a8db554d` 已在 master 谱系上（`git merge-base --is-ancestor a8db554d origin/master` 成立），不再是 #2162 记录的 `5a86841c` 回滚点。本次只替换 `dist`，没有改变这个分支状态，也不影响 #2162 的部署。
- 该漂移在下一次全量部署（含迁移 000059，归 #1986 运维流程）时消除。
- 在那之前，若在主 checkout 里重新 `pnpm build`，会把前端退回 09-09 版本。

## 采集方式

- 本地 Playwright chromium（1440×900）打开线上页面，记录全部请求。脚本：`capture.cjs`，原始输出：`capture.log`。
- 关键截图已缩到 1100 px 宽、JPEG 质量 70 并入库：`03-play-4x.jpg`（4x 播放后）、`05-ifs-unlisted-cycle.jpg`（未列出周期文案）、`06-bootstrap-basins-abort.jpg`（bootstrap 失败提示）。原尺寸 PNG 共 6 张，与完整 `requests.jsonl` 一起留在本地 `.workplans/issue-2336/evidence/`（gitignored）；可用入库的脚本重新生成。
- 分类规则：`/api/v1/tiles/**` 算瓦片；其它 `/api/**` 算 API 请求。

- 时间轴步进会换 `valid_time`，所以 MapLibre 拉取新瓦片是预期行为。验收要求的「零新增请求」针对的是 API 请求，逐项列在下表。
- 「loading 闪烁」用 MutationObserver 统计 `[data-testid="m11-overview-loading"]` 在每个阶段出现的次数。

## 结果

| 验收项 | 结果 | 证据 |
|---|---|---|
| 默认加载冒烟（#2338 删除残骸后） | PASS | 9 个初始 API 请求；`hydro-national` 流量瓦片 6/6 为 200；无 disabled 文案；滑块 0..55 |
| 1x 播放 | PASS：新增 API 请求 0 | 滑块 0→6；新增瓦片 36 个，覆盖 6 个 valid_time；loading 出现 0 次 |
| 4x 播放 | PASS：新增 API 请求 0 | 滑块 6→18；新增瓦片 72 个，覆盖 12 个 valid_time；loading 出现 0 次 |
| 拖动滑块（键盘 ←×6、Home、→×4） | PASS：新增 API 请求 0 | 滑块 18→4；新增瓦片 66 个，覆盖 11 个 valid_time；loading 出现 0 次 |
| 不重发 `/api/v1/layers`、`runs`、`pipeline/status`、queue、basins | PASS | 三个阶段的 API 请求均为 0（`capture.log`） |
| #2127 开放项「每 tick 的 loading 提示闪烁」 | **裁定：未观测到** | 三个阶段共 29 次步进（6 + 12 + 11 次按键），出现次数均为 0 |
| `/?source=ifs&cycle=1999-01-01T00:00:00Z` 文案 | PASS | 文案为 `Cycle 1999-01-01T00:00:00Z is not listed for IFS, so it has no valid times for this layer.`，不是 `Layer has no valid times.` |
| #2139 bootstrap 失败时的提示（可选） | PASS（浏览器端故障注入） | 见下 |

**IFS 未列出周期页的补充：** 页面上仍画着河网，那是纯几何底图 `river-network-national`。

- 该页没有发出任何 `hydro-national/ifs/1999…` 流量瓦片请求（`ifs-unlisted-requests.cjs`）。
- 发出的是 `valid-times?source=ifs&cycle=1999…` 和同一对的 precip index，符合 #2131 设计。

**#2139：** 服务端故障无法无害地复现，改为只在浏览器端注入（`bootstrap-basins-abort.cjs`）：用 Playwright route 让第一个 `/api/v1/basins` 请求失败，服务端不受影响。

- 结果：提示 `m11-overview-empty` 显示 `basins: 暂不可用`。
- 流量层照常渲染：scoped 目录救回了图层，页面也如实显示了 bootstrap 错误。
- 注意：本次注入没有触发阶段 2 再取 basins（只发出 1 个 basins 请求），所以没有覆盖「流域清单被救回」这一分支。它只证明了提示的门控不再依赖流域数，这正是 #2139 修复的那条。

## 顺带观测（不在验收范围，不改代码）

- 默认加载时 `GET /api/v1/precip/gfs/2026-09-13T00:00:00Z/index` 返回 404，页面显示「该周期无降水镜像，降水叠加已隐藏」，说明降水镜像滞后时文案如实呈现。
- 4x 播放 4 s 实际走了 12 步（约 3 步/s，名义 4 步/s），推测是等待瓦片加载所致，未进一步归因。
