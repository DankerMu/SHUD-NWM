# node-27 live receipt — issue #2006 全国河网 paint（PR #2021）

- **取证日期**：2026-09-03（node-27 本地时间 2026-09-04）
- **被测 SHA**：`49e9852e`（rebase 前的首个 commit）；对照组 `origin/master` `a3dc5d69`
- **对最终 head 的有效性**：其后两个 commit 对 `apps/frontend/src/components/map/m11MapPrimitives.tsx`
  的**非注释 diff 行数为 0**，rebase 由 `git range-diff a3dc5d69..7aeb8fba 70337533..HEAD` 证明三个 commit
  逐一等价（三行全部 `=`），因此本 receipt 对合入 SHA `54572e7a` 同样成立。

## 取证方式（未改动生产 bundle）

生产入口 `https://test.nwm.ac.cn` 的 nginx bundle 全程未被触碰。改用独立 git worktree
`/home/nwm/nwm-wt-2006`，在其中分别构建「本分支」与「master」两份 dist，用一次性 Node 脚本做静态
服务并把 `/api` **同源**反代到 live display API（`127.0.0.1:8080`），分别挂 `127.0.0.1:4179` / `:4180`，
Playwright/chromium 在 node-27 本机访问。取证后 worktree、两个服务进程、两份 dist 与 node_modules 均已清理。

数据面是真实的：`/api/v1/layers` 返回 `discharge` / `river-network` / `met-stations`；
河网瓦片走 `/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf`，live 200。

**缩放级别不靠肉眼判断**：脚本监听页面实际发出的瓦片请求，用 URL 里的 `{z}` 自证当前级别。
三次截图对应的河网瓦片 z 集合分别为 `{3}` / `{4,5}` / `{6,7}`。

## 结果（同相机位、同瓦片数据，仅 paint 不同）

| zoom | 变深像素 | 变浅像素 | 结论 |
|---|---|---|---|
| z3 | 5 083 | 1 334 | 河网整体变深变粗；变浅像素集中在抗锯齿边缘 |
| z5 | 12 980 | **0** | 严格变深/变粗，无任何一处变淡 |
| z7 | 136 | 124 | 基本无变化 |

统计口径：灰度化后取地图区（y 80–900、x 260–1340），阈值 ±6。
截图保留在 node-27 `/home/nwm/receipts-2006/`（本分支）与 `/home/nwm/receipts-2006-master/`（对照组）。

z3/z5 的方向与 issue 意图一致：低 zoom 不再乘 0.42、干流线宽上调。

## 未被本次 live 证实的项

`line-opacity` 新增的 `Type 1 → 0.35`（z7）与 `Type 1 → 0.2`、`Type 2 → 0.45`（z6）**无法在本次 live 证实**：
node-27 当前仍是 **v2** 阈值 SQL（z6→Type≥3、z7→Type≥2，`#2005` 未合入），瓦片里根本没有 Type 1，
z6 也不返回 Type 2。z7 几乎无像素变化正是这个原因（且 z7 的 dim 因子改前改后同为 0.42）。
待 #2005 的 v3 SQL 上线后需复测这三项。

## 范围外发现（报告不修）

天地图底图在 node-27 出网侧一律 403（带/不带 `Referer` 均 403，token 在 URL 里），
截图因此是空白底 + 河网/流量矢量。与本 PR 无关，但会影响任何在 27 上取的浏览器视觉 receipt。
