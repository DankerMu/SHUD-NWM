## MODIFIED Requirements

### Requirement: 运维路由保留且 RBAC 不变

`/ops`、`/monitoring`、`/system/model-assets` SHALL 保持可达，且这些路由 MUST NOT 被重定向到单页。`/monitoring` 与 `/system/model-assets` 的 `RBACGate` 角色门控（operator/model_admin/sys_admin 等）MUST 与本变更前一致；`/ops` 仅在 runtime config 同时明确报告 `service_role=display_readonly` 与 `display_readonly=true` 时允许 viewer 渲染只读诊断面，其他运行模式仍保持原角色门控。

#### Scenario: 运维路由仍受 RBAC 门控
- **WHEN** 具备 operator 角色的用户访问 `/ops`
- **THEN** 渲染运维页（非重定向到 `/`），RBAC 行为与变更前一致

#### Scenario: 无权限用户被 RBAC 拒绝
- **WHEN** 无运维角色的用户访问 `/monitoring`
- **THEN** 仍按既有 `RBACGate` 行为拒绝，不因去导航而放宽

#### Scenario: display_readonly viewer 访问只读 ops
- **WHEN** viewer 在 runtime config 同时报告 `service_role=display_readonly` 与 `display_readonly=true` 时访问 `/ops`
- **THEN** 渲染只读诊断面而不重定向到 `/`
- **AND** runtime config 缺失、冲突或不是 display_readonly 时仍按既有 RBACGate 拒绝
