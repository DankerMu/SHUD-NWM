# 展示端移动形态 node-27 实拍 receipt（2026-10-10，#2817）

OpenSpec change `mobile-responsive-display` task 7.2。按 `docs/runbooks/display-mobile-evidence.md`，在 node-27 上以 `mobile-portrait` 与 `mobile-landscape` 两个预设对 live 展示入口 `https://test.nwm.ac.cn` 运行 `scripts/node27_display_v2_browser_evidence.mjs`。两份报告均通过。

## 部署

| 项 | 值 |
|---|---|
| 节点 / 活动 checkout | node-27，`/home/nwm/NWM`（`master`） |
| 部署 commit | `9c2724a18472097b011fbe94c4940789b8f3a8d7` |
| 部署前 commit | `66c9c90351c06753cb0bec58ee9542fb360aa331` |
| 同步方式 | `git status --porcelain` 为空后 `git pull --ff-only` |
| 前端构建 | `corepack pnpm install --frozen-lockfile`（无变化）后 `corepack pnpm exec vite build --outDir dist.next --emptyOutDir`，再把 `dist.next` 换成 `dist` |
| live 入口资产 | 部署前 `assets/index-C7yhQKcq.js`，部署后 `assets/index-D8aMZdyF.js`（`curl http://127.0.0.1:8080/` 读到的与新 `dist/index.html` 一致） |
| 旧 `dist` 备份 | node-27 `~/NWM-presync-backup-2026-10-09/dist` |
| display API | 未重启（两个 commit 之间 `apps/api`、`db`、依赖锁文件无变化；静态文件按请求从磁盘读）。部署后 `/health` 200，公网入口 200 |

部署后的桌面门（同一脚本不带预设，对 `https://test.nwm.ac.cn`）：exit 0，`pass: true`，头部 84、控制条 64，报告的键路径集合与 `docs/runbooks/receipts/2026-09-16-display-v2/window/browser/browser-evidence.json` 110 对 110 一致。

## 命令

pin：`basins_heihe` / `basins_heihe_shud_shud_riv_000001`。运行前按 runbook 确认该 pin 的 segment detail 回 200（`basin_version_id=basins_heihe_vbasins`，`river_network_version_id=basins_heihe_rivnet_vbasins`）。

```bash
cd /home/nwm/NWM
mkdir -p /home/nwm/tmp
OUT=$(mktemp -d /home/nwm/tmp/display-mobile-evidence-XXXXXX)
BASE_URL=https://test.nwm.ac.cn
BASIN=basins_heihe
SEGMENT=basins_heihe_shud_shud_riv_000001

node scripts/node27_display_v2_browser_evidence.mjs --base-url "$BASE_URL" --out-dir "$OUT" \
  --playwright-root /home/nwm/NWM/apps/frontend \
  --device-preset mobile-portrait --river-basin-id "$BASIN" --river-segment-id "$SEGMENT" \
  > "$OUT/mobile-portrait.stdout.json"; echo "mobile-portrait exit=$?"

node scripts/node27_display_v2_browser_evidence.mjs --base-url "$BASE_URL" --out-dir "$OUT" \
  --playwright-root /home/nwm/NWM/apps/frontend \
  --device-preset mobile-landscape --river-basin-id "$BASIN" --river-segment-id "$SEGMENT" \
  > "$OUT/mobile-landscape.stdout.json"; echo "mobile-landscape exit=$?"
```

两条命令都 exit 0。Node v22.22.2；`OUT=/home/nwm/tmp/display-mobile-evidence-naB4YN`。

## 结果

| 项 | `mobile-portrait`（390×664） | `mobile-landscape`（750×342） |
|---|---|---|
| 报告 | `pass: true`，`failures` 为空 | `pass: true`，`failures` 为空 |
| 头部高 | 48 | 48 |
| 地图区 | (0, 48) 390×616 | (0, 48) 750×294 |
| 抽屉（河段窗与气象代站窗相同） | 底部：(0, 265.61) 390×398.39 | 右侧：(375, 48) 375×294 |
| 河段图表区高（下限） | 223.89（160） | 123.5（120） |
| 气象代站图表区高（下限） | 160（160） | 120（120） |
| 河段窗选中锚点 | (195, 156.8)，在抽屉顶边 265.61 之上 | (187.5, 195)，在抽屉左边 375 之左 |
| 气象代站窗选中锚点 | (195, 156.8) | (187.5, 195) |
| 曲线等待 | 河段 206 ms，站点 88 ms，均未超时 | 河段 255 ms，站点 89 ms，均未超时 |
| 轻触的要素 | 河段 `basins_heihe_shud_shud_riv_000001`；站点 `dg-gfs-30fffff33ad6d8dd4591::cell:28107` | 同左 |
| 页面发出的非 GET 请求 | 无 | 无 |

气象代站图表区恰等于下限（160 / 120）：该窗在这两个视口里图表区由下限钉住，是本 change 实现期裁定的取值（tasks.md 里气象代站图表高度的裁定），mocked 回归同样钉在这两个数上。

## 产物

几何 JSON 入库在 `docs/runbooks/receipts/2026-10-10-display-mobile/`；截图不入库（仓库的 receipt 惯例），留在 node-27 的 `/home/nwm/tmp/display-mobile-evidence-naB4YN/`，以 sha256 标识。

| 文件 | sha256 |
|---|---|
| `mobile-mobile-portrait-default.png` | `2be8b8260bcfe4f118206245cd74855d7b78d3071cd844f56b58cad949787759` |
| `mobile-mobile-portrait-river.png` | `044ca42224eaa4271bfaf7891949ae1358d943f7b6f7ef9a8942b9a5dd7b99ae` |
| `mobile-mobile-portrait-station.png` | `93e543f7da723762d8cfb11201e01e376a0a108eb29d529db6d87865d989c7dd` |
| `mobile-mobile-landscape-default.png` | `be5260194a89177e8269df9b8741900904a3cb68be5641cf6d21d6e3df1f50d1` |
| `mobile-mobile-landscape-river.png` | `1c4e0516c333f44306db0247f0930acda053bb2e7569eff375f927b8fd398153` |
| `mobile-mobile-landscape-station.png` | `0094810dee98b27406a90f2e55b409117fa03df733af392255739bb2514efe81` |
| `mobile-geometry-mobile-portrait.json` | `50aded24e6a5b73ccbf7a454fd3321536bcdb887da15c46e004420e71ad89680` |
| `mobile-geometry-mobile-landscape.json` | `cb7e3cba0815e5a01a54498f97d31796bb218b1f182d0f7cb6dba26eb9fccc7c` |

## 限制

- 这是 Chromium 设备模拟，不是真机：浏览器工具栏遮挡、安全区、聚焦自动放大、捏合 / 拖动手势冲突都不在本 receipt 的证明范围内，由 task 7.3（#2818）的真机清单覆盖。
- 截图在 `/home/nwm/tmp/` 下，不保证长期保留；需要长期留存时另行拷出。
- 截图里可见、未判定的两处观察，转 #2818 真机核对：MapLibre 的版权标注叠在抽屉内容之上（竖屏在图表右下角，横屏在图表区右侧）；横屏页面左上角有“已加载 5000 个代站，列表已截断”的提示。

## 更正（2026-10-10，#2865）

- 「限制」里“MapLibre 的版权标注叠在抽屉内容之上”的说法不准确。按层级抽屉在标注之上：抽屉 `M11DraggableCurveWindow` 的 z-index 是 132（激活时 142），MapLibre 控件容器的 z-index 是 2；标注是透过抽屉的半透明底色（`M11_POPUP_GLASS`）被看见的，不是叠在抽屉之上。
- 2026-10-10 在 node-27 上对同一入口、两个预设用 headless shell 与完整 Chromium 各拍一组，河段窗截图里标注同样可见。产物在 node-27 的 `/home/nwm/tmp/2785-receipts/2865-fgate/`。
- 标注是否影响判读由真机清单 RD-15（`docs/runbooks/display-mobile-real-device-checklist.md`）判定。
