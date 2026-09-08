## MODIFIED Requirements

### Requirement: 运维路由与只读边界保留

`/ops` 路由 MUST 保留并可经 role-gated 方式内部访问；当 runtime config 同时明确报告 `service_role=display_readonly` 与 `display_readonly=true` 时，`/ops` 作为只读诊断面 SHALL 允许 viewer 直达，不得使用角色伪装。该例外 MUST NOT 扩展到 `/monitoring` 或 `/system/model-assets`，也不得放宽任何控制行为。display_readonly 的后端边界防护（retry/cancel 返回 `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`、queue-depth `503`、无 Slurm 控制请求）及其测试 MUST 保持不变。

#### Scenario: 内部诊断仍可访问
- **WHEN** 具备 operator/model_admin/sys_admin 角色的用户直接访问 `/ops`
- **THEN** 页面以“内部诊断”形态可访问，不再以“运维工作台”作为业务化主交付

#### Scenario: 只读展示 viewer 可访问诊断页
- **WHEN** runtime config 同时报告 `service_role=display_readonly` 与 `display_readonly=true` 且 viewer 直接访问 `/ops`
- **THEN** 页面渲染只读诊断面
- **AND** `/monitoring` 与 `/system/model-assets` 的既有 RBAC 门保持不变
- **AND** 不显示 role selector、retry 或 cancel 控件

#### Scenario: runtime 缺失或冲突
- **WHEN** viewer 访问 `/ops` 但 config 缺失、加载超过 10 秒或双字段不一致
- **THEN** 按既有 RBAC 拒绝；加载等待有界，晚到的有效 readonly config 可恢复只读访问

#### Scenario: 只读边界不被削弱
- **WHEN** display_readonly 下触发 retry/cancel
- **THEN** 仍返回 `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`，不发 Slurm 控制请求，不写终态
