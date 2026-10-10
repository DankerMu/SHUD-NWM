# evidence-script-followups

## Why

#2815 的复审与 Epic #2785 收尾留下了 node-27 浏览器证据脚本移动预设路径上的几处诊断缺口（#2865），都在失败路径或证据质量上，没有误绿风险：曲线区域崩溃后白等满超时且认不出兜底块；站点坐标的取法与页面不一致；对没有钩子的构建每个状态各白等一个 `--timeout-ms`；runbook 的失败信息表与两处说明不准确；一条用例断言弱于名字。另外，#2817 的 receipt 里“版权标注叠在抽屉内容之上”的说法不准确：按层级抽屉在标注之上（抽屉 z-index 132 / 142，MapLibre 控件容器 z-index 2），标注是透过半透明的抽屉底色被看见的。曾设想让预设路径改用完整 Chromium 来消除这种透出，2026-10-10 在 node-27 上对同一入口、两个预设用 headless shell 与完整 Chromium（`channel: 'chromium'`）各拍一组，河段窗截图里标注同样可见——该设想不成立，不做。

## What Changes

- 脚本（仅 `--device-preset` 路径）：
  - A：曲线区域的错误兜底块 `[data-testid="region-error-map-panels"]` 成为可识别的终态——无论它在窗口 frame 出现之前还是之后出现，都立即结束当前等待，并记一条指名它的失败。
  - B：`stationCandidates` 的坐标取法与页面一致（`geom.coordinates` 前两个有限数优先，否则顶层 `longitude` / `latitude`）。
  - E：钩子存在性等待有自己的上限，实际上限为 min(自身上限, `--timeout-ms`)。
- 单测：A、B 新行为的用例；D：`the chart floor is not judged on top of a curve failure` 钉住失败原文。
- runbook：失败信息表补三类；`emptyText`、分支“互斥”两处说明改准；A、E 对应的表行与说明。
- receipt 更正：`docs/runbooks/receipts/2026-10-10-display-mobile.md` 末尾追加一条“更正（2026-10-10，#2865）”，原句保留；入库的几何 JSON 不动。

不改不带 `--device-preset` 的桌面路径（参数、访问序列、检查、报告键、截图名、退出码、浏览器启动方式；`main()` 不改）；不新增 CLI 参数；不增删报告键（schema `nhms.node27-display-mobile-evidence.v1` 不变）；不改任何前端代码。“相机静止判定过早”未确认，不纳入。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (agree；合同里 script entry / legacy 是 expanded 触发词，这里维持 compact 的理由：不加参数、不增删报告键、`geometry.panel` 只多一个取值且仓库内无消费者、`main()` 与默认路径一行不改，改动全部落在预设路径的失败诊断上)
Blast radius: 移动实拍的失败诊断与截图质量；改坏时最坏是移动预设在 node-27 上跑不起来或误红，桌面 oracle 若被波及则每个展示端 PR 的 receipt 都受影响
Selected risk packs: Error handling; Legacy compatibility; Documentation
Evidence floor: 单测全绿且不少于 59 条；桌面路径函数体与 origin/master 哈希相同；node-27 上不带预设 exit 0 且键路径 110/110、两个预设对 live 入口 pass
```

## Impact

- 受影响文件：`scripts/node27_display_v2_browser_evidence.mjs`、`scripts/__tests__/node27_display_v2_browser_evidence.test.mjs`、`docs/runbooks/display-mobile-evidence.md`、`docs/runbooks/receipts/2026-10-10-display-mobile.md`。
- 受影响规格：`mobile-regression-evidence`。
