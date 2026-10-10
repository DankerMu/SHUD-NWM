# 展示端移动形态实拍证据（node-27）

用 `scripts/node27_display_v2_browser_evidence.mjs` 的设备预设，对 live 展示入口采集 `/` 在移动形态下三种状态（默认 / 河段窗 / 气象代站窗）的截图与几何 JSON。对应 OpenSpec change `mobile-responsive-display` 的 task 7.1（脚本，#2815）与 7.2（receipt）；规格见 `specs/mobile-regression-evidence` 的「Live mobile evidence SHALL be captured on node-27」。

脚本对站点只读：只发 GET，只做导航、每个开窗状态一次触摸轻触与读取，不调用任何产品写接口。带预设时它在页面启动前设 `window.__NHMS_E2E_HOOKS__ = true`，让两个只读定位钩子（`__nhmsRiverClickEvidence.locateRenderedRiver`、`__nhmsStationLocateEvidence.locateRenderedStation`）存在；钩子只定位、不调用产品回调，窗口只能由那一次真实轻触打开。

不带 `--device-preset` 时脚本的参数、访问序列、检查、报告结构、截图文件名与退出码都和以前一样（桌面 oracle，见 `docs/runbooks/receipts/2026-09-16-display-v2.md`），本文不涉及。

## 前置

- **在 node-27 上跑**（oracle 路由见根 `CLAUDE.md`）。先过主机容量纪律（`docs/runbooks/node-27-bringup-checklist.md` 的「主机容量纪律」）。
- **Playwright 根目录**：`--playwright-root /home/nwm/NWM/apps/frontend`（该目录的 `node_modules` 里有 playwright 与已装好的 Chromium；脚本只用 Chromium）。
- **输出目录**放在仓库外的私有目录（脚本不在仓库里留任何文件）；确认为 receipt 的文件再拷进 `docs/runbooks/receipts/`。
- **河段 pin（两个，必填，由操作者指定，脚本不自动挑河段）**：沿用 `docs/runbooks/node-27-bringup-checklist.md`「C4-river-click」一节的既有约定——`--river-basin-id` 是流域 `basin_id`，`--river-segment-id` 是 discharge 图层实际渲染的 id 族（当前即 `<basin_id>_shud_shud_riv_000001`）。使用前对该 pin 的 segment detail 发一次 GET，必须 200：

  ```bash
  BASE_URL=https://test.nwm.ac.cn
  BASIN=<basin_id>
  SEGMENT=<river_segment_id>
  PRODUCT=$(curl -fsS "$BASE_URL/api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=$BASIN")
  BV=$(printf '%s' "$PRODUCT" | sed -E 's/.*"basin_version_id":"([^"]+)".*/\1/')
  RNV=$(printf '%s' "$PRODUCT" | sed -E 's/.*"river_network_version_id":"([^"]+)".*/\1/')
  curl -sS -o /dev/null -w '%{http_code}\n' "$BASE_URL/api/v1/basin-versions/$BV/river-segments/$SEGMENT?river_network_version_id=$RNV"
  ```

  **不要**从 `/api/v1/basin-versions/{bv}/river-segments?limit=1` 的清单里取 pin：清单返回的是 `…_shud_seg_…` id（segment detail 对它回 404）与 `properties.reach_segment_id` 的 `…_shud_reach_…` id（detail 回 200，但 discharge 图层不渲染它，钩子以 `HOOK_FEATURE_MISMATCH` 拒绝）。2026-10-09 对公网入口实测如此。
- **选流域时同时要满足气象代站一侧的两个条件**（河段与气象代站共用同一个流域 pin）：
  1. 页面只为前 50 个流域取站点，且全图层在 5000 站处截断（页面左上角会提示“已加载 5000 个代站，列表已截断”）；流域按 `basin_id` 顺序取数，排在截断之后的流域没有站点要素，钩子对它的每个候选都回 `STATION_HOOK_NOT_RENDERED`。
  2. 嵌套 / 重叠流域的代站坐标重合（同一格点上叠着另一个流域的站点），钩子回 `STATION_HOOK_POINT_OCCLUDED`；这类流域的 `…_riv_000001` 河段也常与另一张河网的河段重合，河段钩子回 `HOOK_POINT_OCCLUDED`。

  所以选一个 `basin_id` 排序靠前、不与其他流域重叠的流域。2026-10-09 实测可用的一组：`basins_heihe` / `basins_heihe_shud_shud_riv_000001`（`basins_byh` 因重叠两侧都被拒，`basins_qhh` 排在 5000 站截断之后）。产品身份会变，这组值每次使用前按上面的命令重新确认。
- **站点**默认由脚本自己取：用 GFS latest-product 的 `model_id` 请求 `/api/v1/met/stations?basin_version_id=<bv>&model_id=<model_id>&limit=200`（按 `offset` 翻页取全），按 `station_id` 排序后取前 5 个有坐标的依次尝试。要钉住某一个站点时加 `--station-id <station_id>`（必须在同一份清单里）。

## 两个预设

| 预设 | 视口 | DPR | 触屏 | isMobile | 形态 | 抽屉位置 | 图表区下限 |
|---|---|---|---|---|---|---|---|
| `mobile-portrait` | 390×664 | 2 | 是 | 是 | 移动形态 | 底部 | 160px |
| `mobile-landscape` | 750×342 | 2 | 是 | 是 | 移动形态（矮视口横屏） | 右侧 | 120px |

两者都跑 Chromium，UA 为 `Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36`。视口与 mocked 回归车道的同名 project 相同。

## 命令

```bash
cd /home/nwm/NWM
OUT=$(mktemp -d /home/nwm/tmp/display-mobile-evidence-XXXXXX)
BASE_URL=https://test.nwm.ac.cn
BASIN=<basin_id>
SEGMENT=<river_segment_id>

node scripts/node27_display_v2_browser_evidence.mjs --base-url "$BASE_URL" --out-dir "$OUT" \
  --playwright-root /home/nwm/NWM/apps/frontend \
  --device-preset mobile-portrait --river-basin-id "$BASIN" --river-segment-id "$SEGMENT" \
  > "$OUT/mobile-portrait.stdout.json"; echo "mobile-portrait exit=$?"

node scripts/node27_display_v2_browser_evidence.mjs --base-url "$BASE_URL" --out-dir "$OUT" \
  --playwright-root /home/nwm/NWM/apps/frontend \
  --device-preset mobile-landscape --river-basin-id "$BASIN" --river-segment-id "$SEGMENT" \
  > "$OUT/mobile-landscape.stdout.json"; echo "mobile-landscape exit=$?"
```

参数说明：

- `--device-preset mobile-portrait|mobile-landscape`：选预设；未知名字是用法错误，报错信息列出合法取值。
- `--river-basin-id`、`--river-segment-id`：带预设时必填，须匹配 `[A-Za-z0-9._:-]{1,96}`。
- `--station-id`：可选。
- `--timeout-ms`（默认 60000）：每一步等待的上限（导航、钩子出现、图层就绪、曲线加载、相机静止）。
- `--viewport` 不能和 `--device-preset` 同时给（视口由预设决定）；`--river-basin-id` / `--river-segment-id` / `--station-id` 不带预设时不能出现。这些都是用法错误。
- `--settle-budget-ms` / `--settle-samples` 在预设路径不参与判定：各状态的 `settle_ms` 只记录。

退出码：`0` 报告通过；`1` 至少一条失败（文件照常产出）；`2` 用法错误或浏览器启动失败（不产出报告）。

## 输出文件

每个预设在 `--out-dir` 下写四个文件，不覆盖桌面路径的 `browser-evidence.json` 与四张 `overview-*.png`：

| 文件 | 内容 |
|---|---|
| `mobile-<preset>-default.png` | 默认状态（`/`）的视口截图 |
| `mobile-<preset>-river.png` | 河段窗打开后的视口截图 |
| `mobile-<preset>-station.png` | 气象代站窗打开后的视口截图（`/?metStations=1`） |
| `mobile-geometry-<preset>.json` | 几何报告，schema `nhms.node27-display-mobile-evidence.v1`；同样的内容打印到 stdout |

报告的顶层键：`schema`、`base_url`、`preset`（预设全量）、`form`、`expectations`（按形态选出的期望）、`river_target` / `station_target`（从展示 API 取到的定位目标，取不到时是 `{ "error": … }`）、`captured_at`、`states`、`non_get_requests`、`failures`、`pass`。

`states.default` / `states.river` / `states.station` 各含 `url`、`settle_ms`、`tap`（轻触的视口点与目标 id）、`attempts`（每次钩子调用的结果与错误码）、`geometry`、`screenshot`、`failures`。`geometry` 含视口、头部盒、控制条盒、三个启动器盒、地图区盒、曲线窗 frame 盒（`sheet`）、图表区盒（`chart`）、`anchor`（`data-selected-anchor-x/-y` 原文）、`camera`，以及该状态下全部可见可点控件的盒（每个带“是否在曲线窗内”“是否在纵向滚动容器内”与可见纵向区间）。

`non_get_requests` 记录三个页面发出的全部非 GET 请求，正常为空数组——它是“只读”的旁证，不参与判定。

三个状态各用一个新页面、互不依赖：某个状态采集失败（钩子超时、live 上曲线没数据等）时，其余状态照常采集，三张截图照常产出，报告为不通过。**“采集路径跑通”（四个文件都在）与“报告通过”（`pass: true`）是两回事。**

## 通过条件

`pass` 为真当且仅当 `failures` 为空。判定逐条如下（全部是几何与属性判定；不使用文档滚动宽度，不做截图像素比对）。

默认状态：

- 头部高 48px；
- 底部控制条存在，且它的盒在视口内（移动形态不钉控制条高度）；
- 三个启动器（图层 / 底图 / 图例）都存在；
- 全部可见可点控件的盒在视口内。

河段窗、气象代站窗两个状态各自：

- 曲线窗 frame 存在；
- 图表区存在且高度不低于下限（`mobile-portrait` 160px，`mobile-landscape` 120px）。图表区不存在时分两种失败：曲线到超时仍在加载、加载结束但没有曲线数据；
- 地图容器上有两个选中锚点属性；
- 锚点在地图区内，且在未被抽屉遮住的一侧——底部抽屉：锚点 y 小于抽屉顶边；右侧抽屉：锚点 x 小于抽屉左边；
- 曲线窗内全部可见可点控件的盒在视口内。

“可见可点控件”的口径与 mocked 车道的全页触控审计相同：跳过宽或高为 0、`display: none`、`visibility` 非 visible、处在 `[inert]` / `[aria-hidden="true"]` 子树里的元素与 `canvas`；被非滚动的裁剪祖先完全裁掉的跳过；纵向滚动容器（抽屉主体）的成员横向判完整盒、纵向只判可见区间，完全滚出算通过；边缘容差 0.5px；MapLibre attribution 豁免。

量几何之前脚本先等相机静止：间隔 650ms 的连续两次读数里，`data-selected-anchor-x/-y`、`data-camera-center`、`data-camera-zoom` 四个属性都相同（平移动画 450ms，锚点属性只在相机静止后更新）。等不到时该状态记一条失败。

## 失败信息对照

| 失败信息里的关键字 | 含义 | 处置 |
|---|---|---|
| `HOOK_POINT_OCCLUDED` | 河段定位点被遮挡：锚点落在控制条或启动器下，或该点的产品点击会落到另一个要素上 | 换一个河段 pin |
| `HOOK_FEATURE_MISMATCH` | discharge 图层在该点渲染的不是这个 id | pin 用 `…_shud_riv_…` id 族，见「前置」 |
| `no station could be located after N candidate(s)`，全部 `STATION_HOOK_NOT_RENDERED` | 该流域没有站点要素（排在前 50 个流域或 5000 站截断之后） | 换一个流域 pin |
| 同上，含 `STATION_HOOK_POINT_OCCLUDED` / `STATION_HOOK_CLUSTERED` | 候选站点被另一个流域的重合站点盖住或被聚合 | 换一个不重叠的流域，或用 `--station-id` 指定一个 |
| `is missing after … ms (does the deployed build carry the hook?)` | 被测入口的构建里没有该钩子 | 被测入口尚未部署到含钩子的 commit；不是脚本问题 |
| `the station layer has no features` | `data-met-station-feature-count` 在超时内一直是 0（页面要为最多 50 个流域串行取数后才写入） | 加大 `--timeout-ms` 重跑；仍为 0 查站点接口 |
| `latest-product … is unavailable` | 流域 pin 没有 GFS 展示产品；河段与气象代站两个状态都记失败，默认状态照常采集 | 换流域 pin |
| `curve still loading at the timeout` / `no curve data` | 曲线到超时还在加载 / 加载完但 live 上没有曲线数据 | 前者加大 `--timeout-ms`；后者换 pin 或查序列接口 |
| `header height 84 != 48`、`launcher … is missing` | 被测入口还是桌面布局 | 被测入口尚未部署移动形态改动 |

## receipt 应记录的内容

规格「Receipt content」要求 receipt 写明：

- 两条完整命令行（含实际的 `--base-url`、两个 pin、`--station-id`（若给了）与 `--timeout-ms`（若改了））；
- 被测入口部署的 commit（node-27 上 `git -C /home/nwm/NWM rev-parse HEAD`，以及展示前端构建所对应的 commit）；
- 每个预设的三张截图与几何 JSON 的入库路径（`docs/runbooks/receipts/<日期>-display-mobile/…`）；
- 每个预设的退出码、`pass` 与 `failures` 原文；河段窗与气象代站窗的图表区高度（`states.river.geometry.chart.height`、`states.station.geometry.chart.height`）；
- pin 的来源（segment detail 的 200 响应、选该流域的理由）。

## 已知限制

- 这是 Chromium 的设备模拟：视口、DPR、触屏与 UA。模拟器里 `dvh == vh`、`env(safe-area-inset-*)` 为 0，证明不了浏览器工具栏遮挡、安全区、聚焦自动放大、捏合 / 拖动手势冲突——这些归真机确认清单（task 7.3，`docs/runbooks/display-mobile-real-device-checklist.md`）。
- 河段目标的取法是 live-river-click 车道 preflight 的简化：同一组只读请求（GFS latest-product -> segment detail），但不做 IFS 交叉核对，也不做字节预算与重定向约束。本脚本是证据采集，不是合并门。
- 钩子定位会把相机移到目标处并放大，所以开窗状态的截图是放大后的局部，不是全国视图。
- 页面只为前 50 个流域取站点、全图层 5000 站截断：见「前置」。
- 单测 `node --test scripts/__tests__/node27_display_v2_browser_evidence.test.mjs` 覆盖预设解析、参数解析、形态与期望、移动判定与报告组装；它目前不在 CI 里，改脚本后手动跑。
