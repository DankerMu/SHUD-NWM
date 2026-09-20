# Spec Delta: responsive-screenshot-evidence

## ADDED Requirements

### Requirement: Live browser evidence MUST declare its origin's authorization state for external map providers

含外部底图 provider 的 live browser 证据 SHALL 记录其运行来源，并 SHALL 声明该来源相对
provider client key 授权白名单的状态；receipt SHALL NOT 包含 key 值。

node-27 的 live browser 证据依赖外部底图 provider（天地图 WMTS），而该 provider 的
client key 按**域名白名单**授权。同一份 built app 在受许可来源与隔离 loopback 来源上
会得到不同的 provider 响应，因此 receipt 必须声明自己跑在哪种来源上，并据此说明
`m11-map-source-error` 横幅的出现是预期结果还是回归。

缺少这条声明时，隔离来源上的 provider 授权失败会被读成应用回归，或反过来被当作
公网生产健康的证据——两种误读都发生过（见 #2436）。

**与 C4 判定的关系（carve-out）**：本要求 SHALL NOT 放松
`c4-live-display-evidence` 的「任何 required failure、source error、permission/runtime
错误、超时、Slurm 请求或非 GET/HEAD 控制请求 MUST 阻止 PASS」。C4 receipt 绝不允许
把可见的 `m11-map-source-error` 标注为「预期」后仍宣称 PASS——
`apps/frontend/playwright.c4-display-lane.ts` 见到 `observation.mapSourceError`
即返回 `HOME_NOT_READY`，这条闸门保持原样。本要求只约束 receipt 必须**声明来源的授权状态**，
其效果是：未获 provider 许可的来源根本不是 C4 的合法执行来源，而不是让它带着横幅通过。

#### Scenario: Receipt declares provider authorization for the origin it ran on

- **WHEN** 一份 node-27 live browser receipt 记录了含外部 provider 底图的地图路由
- **THEN** 该 receipt SHALL 记录本次运行的来源（origin），并声明该来源是否在 provider key 的
  授权白名单内
- **AND** 若来源不在白名单内，receipt SHALL 把可见的 `m11-map-source-error` 横幅标注为该来源的
  **预期** provider 授权结果（附 provider 返回的错误码），并 SHALL 声明该次运行**不构成** C4 PASS
- **AND** 若来源在白名单内，receipt SHALL 记录底图层与注记层的实测 HTTP 状态、横幅不可见、
  以及 provider attribution 可见
- **AND** receipt、日志与截图 SHALL NOT 包含 provider client key 的值
