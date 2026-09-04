# node-27 live receipt — issue #2004 / PR #2020：V2.0 品牌 header（84px）

- 日期：2026-09-04
- Issue：#2004（Epic #2003，m27）· PR：#2020
- 被验证提交：`d005fd4a5702671855898c54d7364438f242d3e1`（PR 冻结 SHA）
- 节点：node-27（`nwm@210.77.77.27:32099`，`/home/nwm/NWM`）
- 入口：`https://test.nwm.ac.cn`（27 nginx 反代到本机 `127.0.0.1:8080`，无 SSH 隧穿）
- 触发原因：CLAUDE.md 验证路由表要求 display/前端改动产 node-27 live receipt。issue #2004 验收条款 3
  原本允许「本地自查、截图随 I15 汇总」，维护者要求本 issue 先取实机 receipt 再合并，故本 receipt
  在 I15 的汇总 receipt 之外单独产出。

## 部署过程

```
cd /home/nwm/NWM
git status --porcelain      # 11 项，全部 untracked（.entropy-baseline/*、node27-1069-provenance-*、pytest-of-nwm/），0 项 tracked
git fetch origin
git checkout -B feat/issue-2004-display-v2-brand-header origin/feat/issue-2004-display-v2-brand-header
git rev-parse HEAD          # d005fd4a5702671855898c54d7364438f242d3e1
export PATH=$HOME/.local/bin:$PATH
cd apps/frontend && corepack pnpm install --frozen-lockfile && corepack pnpm build   # ✓ built in 16.91s
cd /home/nwm/NWM && bash scripts/ops/start-display-api.sh
```

切换前 node-27 在 `master@1b56648b`，无 tracked 改动，故分支切换不吞任何本地工作。

display API 重启输出（脚本自带 smoke check）：

```
[start-display-api] target=127.0.0.1:8080  log=/tmp/display-api.log workers=2
[start-display-api] stopping prior uvicorn pid(s): 1282659
[start-display-api] systemd relaunched main_pid=2464060
[start-display-api] OK systemd_main_pid=2464060 workers=2 basin_id=basins_wj (smoke check passed)
```

## 构建产物核查（部署前，node-27 上 `apps/frontend/dist/`）

| 检查 | 结果 |
|---|---|
| 全角标题串在 JS 包内 | `assets/index-BgB8YKG-.js` 含 `全国水文模拟系统（V2.0）` |
| `h-[84px]` CSS 规则 | `assets/index-CZEyHhCN.css: h-\[84px\]{height:84px}` |
| `text-[28px]` | 存在于 `assets/index-CZEyHhCN.css` |
| `font-extrabold` | 存在（1 处） |
| `h-14` | `h-14{height:calc(var(--spacing) * 14)}`，`--spacing:.25rem` → 56px |
| 旧 `h-[68px]` | 0 命中（已消失） |

## 实机浏览器验证（`agent-browser` → `https://test.nwm.ac.cn/`）

服务身份先行确认：`GET /api/v1/runtime/config` → `service_role=display_readonly`、
`control_mutations_enabled=false`、`slurm_routes_enabled=false`，只读边界未因本次部署改变。
`GET /health` → 200。`GET /api/v1/layers` → 200 且返回 live `discharge` 条目。

| 视口 | header 高度 | 标题精确匹配 | font-weight | font-size | 赞助商高度 | 赞助商右缘 | 无横向滚动 | main 高度 |
|---|---|---|---|---|---|---|---|---|
| 1920×1080 | 84px | true | 800 | 28px | 56px | 1900 | true | 996 = 1080−84 |
| 1440×900 | 84px | true | 800 | 28px | 56px | 1420 | true | 816 = 900−84 |
| 1280×900 | 84px | true | 800 | 28px | 56px | 1260 | true | 816 = 900−84 |
| 1024×768（`lg` 断点） | 84px | true | 800 | 28px | 56px | 1004 | true | 684 = 768−84 |

- 「标题精确匹配」= `textContent === '全国水文模拟系统（V2.0）'`（全角 U+FF08/U+FF09）在浏览器内求值为 true。
- 赞助商 `object-fit` 实测为 `contain`。
- 标题块右缘各视口均为 x=429.56；1024 下与赞助商左缘（1004−333.7=670.3）间距 240px，不重叠。
- 各视口 `document.body.scrollWidth <= window.innerWidth` 均为 true。
- main 高度在四档均精确等于 `innerHeight − 84`，证明应用壳按 84px header 正确推导可用高度。

路由 smoke：

| 路径 | 结果 |
|---|---|
| `/ops` | header 84px、无横向滚动 |
| `/hydro-met` | 重定向到 `/`（旧别名行为不变） |

`agent-browser errors` 与 `console` 无页面错误、无 WebGL 报错。

## 覆盖范围与不覆盖

覆盖：`national-overview-page` 的 `Header brand identity for V2.0` 两个 scenario（标题文案与字重字号、
赞助商 ≥56px 且 `object-contain`、header 恰为 84px 且不裁切）；`map-first-layout-conformance` 布局
oracle 中本 issue 拥有的两半——顶栏实测 84px、无横向 body 滚动。

不覆盖（明确不由本 receipt 闭合）：64px 底部控制条（I12）、浮层位移（I11）、降水叠加与时间轴、
河网密度。地图 canvas 内的要素渲染不在本 receipt 判定范围内（本 receipt 的对象是 header 与壳布局），
无头浏览器截图中地图为空白，但未记录任何页面错误，且 `/api/v1/layers` 返回 live 数据。
本 receipt 不是 C1–C4 完整上线 receipt，只闭合 C4 中与 84px header 相关的部分。

## 收尾

合并后 node-27 应回到 master：

```
cd /home/nwm/NWM && git checkout master && git pull --ff-only
cd apps/frontend && corepack pnpm build && cd /home/nwm/NWM && bash scripts/ops/start-display-api.sh
```
