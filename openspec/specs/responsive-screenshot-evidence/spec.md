# responsive-screenshot-evidence Specification

## Purpose
TBD - created by archiving change m15-frontend-visual-conformance. Update Purpose after archive.
## Requirements
### Requirement: Responsive screenshot evidence
Visual conformance SHALL produce screenshot evidence for named routes and viewport matrix.

#### Scenario: Evidence capture
WHEN screenshots are captured
THEN artifacts include route, viewport, fixture mode, real commit SHA, and expected state label

#### Scenario: Required route matrix
WHEN visual conformance evidence is generated
THEN loaded-state screenshots exist for overview and monitoring at 1920x1080, 1440x900, and 1280x900

#### Scenario: Extended route matrix
WHEN deterministic fixtures exist for completed M12-M14 surfaces
THEN `/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1`, `/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=114.35&gridQueryLat=30.62`, `/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237`, and `/system/model-assets` are included in evidence or documented as remaining surfaces with a concrete reason

#### Scenario: Canonical state viewport
WHEN non-happy state evidence is generated
THEN required and feasible extended state labels are captured at canonical 1440x900 while loaded required routes retain 1920x1080, 1440x900, and 1280x900 coverage

#### Scenario: Overlap check
WHEN screenshot route renders dynamic content
THEN test asserts key panels/buttons/text do not overlap incoherently

#### Scenario: Bounded artifacts
WHEN screenshot capture writes local evidence
THEN it writes only under the documented issue evidence directory and records a manifest entry for each artifact

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

