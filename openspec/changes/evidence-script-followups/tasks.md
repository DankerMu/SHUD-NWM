# Tasks

- [x] 1.1 A：区域兜底为可识别终态，两种时机都覆盖。
  - frame 出现之前（曲线区域首次渲染即崩溃，frame 根本不会出现）：`openCurveWindow` 里等 frame 可见的那一步在兜底块出现时立即结束，由采集层直接记一条指名 `region-error-map-panels` 的失败（此路径不进 `judgeMobileState`），不再等满窗口上限后报 `did not appear`。
  - frame 出现之后：读数把兜底块记进现有的 `geometry.panel`（新取值 `region-error-map-panels`），曲线等待在它出现时立即结束；`judgeMobileState` 在“frame 缺失”的提前返回之前判出，失败信息指名该兜底块，且该状态不再报 `curve still loading` 或 `curve window frame is missing`。
  - 两种时机的失败信息用同一措辞。无兜底块时 `curve window frame is missing` 与 `did not appear within` 的既有行为保留。
- [x] 1.2 B：`stationCandidates` 坐标优先级与 `apps/frontend/src/lib/hydroMet/runtime.ts` 的 `getHydroMetStationCoordinates` 一致（`geom.coordinates` 前两个有限数优先，非有限数时回退顶层 `longitude` / `latitude`）；输出形状不变。
- [x] 1.3 E：钩子存在性等待取 min(自身上限, `--timeout-ms`)，自身上限不大于 `LOCATE_TIMEOUT_MS`、不小于窗口出现的上限；失败信息里的毫秒数是实际上限。
- [x] 1.4 单测：A 的合成几何用例（有兜底块 -> 恰一条指名它的失败、无 `curve still loading`；无兜底块且无 frame -> 既有信息）；B 的四种输入（只有 `geom`、两者都有且不同取 `geom`、只有顶层字段、`geom.coordinates` 非有限数回退顶层）；D：`the chart floor is not judged on top of a curve failure` 的第一条断言改 `deepEqual` 钉原文；E 若能落在导出的纯函数 / 常量上则加一条。除 D 那一条外，既有用例一条不改通过。
- [x] 1.5 runbook：失败信息表补 `river segment … is unavailable`、`station list for basin … is unavailable`（含 `--station-id … is not in the list` 与 `no station with coordinates among N`）、`screenshot failed: …`，以及 A 的兜底行（处置：看截图里的“此区域加载失败”与浏览器控制台，按产品 bug 上报，加大 `--timeout-ms` 无效）；`emptyText` 写成“折叠空白后截到 120 字符”；“各分支互斥”改准（`m11-station-panel-refreshing` 与图表区 / 空态并存，出现时仍按“仍在加载”继续等）；E 的上限写进 `--timeout-ms` 说明与对应表行。“CI 车道”那两条 bullet 里只允许改“移动判定（含曲线等待结果的两种失败）”这半句使其与 A 一致，其余不动。
- [x] 1.6 receipt 更正：在 `docs/runbooks/receipts/2026-10-10-display-mobile.md` 末尾追加一节“更正（2026-10-10，#2865）”，原文一字不改。内容只写已核实的事实：(a)「限制」里“版权标注叠在抽屉内容之上”不准确——按层级抽屉在上（`M11DraggableCurveWindow` 的 z-index 132 / 142，MapLibre 控件容器 z-index 2），标注是透过抽屉的半透明底色（`M11_POPUP_GLASS`）被看见的；(b) 2026-10-10 在 node-27 上对同一入口、两个预设用 headless shell 与完整 Chromium 各拍一组，河段窗截图里标注同样可见（产物在 node-27 `/home/nwm/tmp/2785-receipts/2865-fgate/`）；(c) 标注是否影响判读由真机清单 RD-15 判定。不写未入库、不可复核的结论。

## 约定

- Risk pack「Error handling」selected：-> 1.4 的 A 用例；frame 之前时机由注入实跑证明（见 Evidence floor）；E 由纯函数用例或代码路径 + node-27 实跑为证，报告里说明是哪一种。
- Risk pack「Legacy compatibility」selected：默认路径不变 -> `main`、`visit`、`checkScene`、`loadChromium` 四个函数体对 `origin/master` 哈希相同；`parseArgs` 对既有参数的差分一致；node-27 上不带预设跑一次，exit 0、键路径集合与 `docs/runbooks/receipts/2026-09-16-display-v2/window/browser/browser-evidence.json` 110 对 110。
- Risk pack「Documentation」selected：-> 1.5、1.6；markdownlint 通过；runbook 里的信息与脚本实际字符串逐字一致（审查项）。
- 未选：Public API / CLI（不加参数、不增删报告键；`geometry.panel` 多一个取值，仓库内无消费者）、Config、File IO、Schema、Auth、Concurrency、Resource limits、Release / dependency（不加依赖、不换浏览器通道）。
- Must preserve：见 Legacy compatibility；另：入口守卫与 stdin 行为；`non_get_requests` 仍为空；CI 的 `Script Node Tests` 车道不装 Playwright，单测不得在顶层 import playwright。
- 行数：脚本现 997 行，超过 1000 行会被结构预算归为 `mandatory-governance`。精简只许动预设路径的代码与注释，不碰 `main` / `visit` / `checkScene` / `loadChromium` / `parseArgs` 的既有分支；精简后仍超 1000 行就停下上报，不自行拆文件。
- Non-goals：相机静止判定；前端代码与钩子；新增 CLI 参数；更换浏览器通道（已实测无效，见 proposal）；站点截断、河段 id 族；重新采集并替换 #2817 的截图；判断版权标注透出是不是产品问题。
- Evidence floor：
  - `node --check`；`node --test scripts/__tests__/node27_display_v2_browser_evidence.test.mjs`（显式文件路径）全绿，用例数 = 59 + 新增数，报告分组计数；CI 形式 `shopt -s failglob; node --test scripts/__tests__/*.test.mjs` 也绿。
  - 变异（按 sha256 还原）：`judgeMobileState` 里曲线超时与图表下限的优先级对调 -> D 用例红；去掉兜底判定 -> A 用例红；坐标优先级改回只读顶层 -> B 用例红。
  - 默认路径哈希 / 差分（见上）。
  - A 的 frame 之前时机：本分支前端构建 + 只读代理，对脚本打一个**不提交**的临时补丁，在页面启动前设既有的崩溃注入开关（`RegionCrashProbe` 读的那个 `window` 变量，取值 `curve`），带预设实跑一次：预期开窗状态失败信息指名 `region-error-map-panels`，该状态耗时远小于窗口上限 10 秒，其余状态照常；PR 里贴补丁与结果。frame 之后的时机只有单测与代码路径证据，如实写明。
  - `uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` pass；markdownlint 两份文档 0；`openspec validate evidence-script-followups --strict --no-interactive`。
  - node-27（编排者执行）：单测；不带预设对 live 入口 exit 0 且键路径 110/110；两个预设对 `https://test.nwm.ac.cn` pass、`non_get_requests` 为空。
