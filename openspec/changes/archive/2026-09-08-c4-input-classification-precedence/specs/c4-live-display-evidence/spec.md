## MODIFIED Requirements

### Requirement: 独立真实 C4 入口

系统 SHALL 提供 `test:e2e:live-c4-display`，要求显式 frontend/API origin、basin、segment、receipt path。入口 MUST NOT 使用 mock/HAR API、角色 override、历史 receipt 或 river-click receipt 代替真实 C4，普通测试发现 MUST 排除 live-only lane。输入错误分类顺序 MUST 为 receipt-path preflight → frontend/API URL presence → basin/segment pin validity；receipt path 或 URL 缺失产生的 BLOCKED MUST 优先于同次存在的 pin 错误。

#### Scenario: 缺失输入不能变成跳过后成功
- **WHEN** frontend/API origin 或 receipt path 缺失
- **THEN** 入口非零结束为 BLOCKED，即使 basin/segment pin 同时缺失、空白或非法；有可安全发布的路径时提供 BLOCKED 证据，不报告 PASS

#### Scenario: 产品定位 pin 缺失或非法
- **WHEN** receipt path、frontend origin 与 API origin 均已提供，但 basin 或 segment pin 缺失、空白或不符合标识符约束
- **THEN** 入口非零结束为 FAIL CONFIG_INVALID，并在路径可安全发布时提供 FAIL 证据，不启动浏览器、不报告 PASS

#### Scenario: 真实双源证明
- **WHEN** 显式配置就绪并执行 C4
- **THEN** Chromium 访问真实 `/` 和 GFS/IFS strict `/ops`，以 source、basin/version、network version、run、model、cycle、scenario 与 job/log identity 关联实际观察
- **AND** 普通 Vitest 的 fake-page/VM 测试不要求浏览器二进制，也不构成 live PASS
