## ADDED Requirements

### Requirement: display_readonly 下运维入口降级

在 `display_readonly` 运行模式下，主导航 MUST NOT 显示 `/ops` 与 `/monitoring` 入口；在 `compute_control`/`dev_monolith` 模式下 SHALL 保持现有导航不变。

#### Scenario: 只读节点隐藏运维入口
- **WHEN** 前端运行于 `display_readonly`（由 runtime config 报告）
- **THEN** 主导航不出现 `/ops` 与 `/monitoring` 入口

#### Scenario: 计算/开发模式保持导航
- **WHEN** 前端运行于 `compute_control` 或 `dev_monolith`
- **THEN** `/ops` 与 `/monitoring` 导航行为与本变更前一致

### Requirement: /monitoring 降级保持路由与语义

在 `display_readonly` 下 `/monitoring` 同 `/ops` 一并从主导航降级；其路由 MUST 保留、诊断/只读语义 MUST 保持现状，MUST NOT 因隐藏导航而删除路由或改变既有访问行为。

#### Scenario: /monitoring 隐藏导航但路由保留
- **WHEN** 前端运行于 `display_readonly`
- **THEN** 主导航不出现 `/monitoring` 入口，但 `/monitoring` 路由仍可直接访问且只读语义不变

### Requirement: /meteorology 合同门控保持不变

本变更 MUST NOT 改变 `/meteorology` 的现有合同门控行为（`hasMinimumMeteorologyContracts()`）：合同未就绪时不作为生产化入口，就绪时保持现有行为。改动 `NavBar`/`App` 时 MUST NOT 误删或绕过该门控。

#### Scenario: 合同未就绪时仍不展示
- **WHEN** minimum meteorology contracts 未满足
- **THEN** `/meteorology` 不出现在主导航、不作为生产化入口，与本变更前一致

#### Scenario: 合同就绪时行为不变
- **WHEN** minimum meteorology contracts 满足
- **THEN** `/meteorology` 的现有展示/导航行为保持不变

### Requirement: 运维路由与只读边界保留

`/ops` 路由 MUST 保留并可经 role-gated 方式内部访问；display_readonly 的后端边界防护（retry/cancel 返回 `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`、queue-depth `503`、无 Slurm 控制请求）及其测试 MUST 保持不变。

#### Scenario: 内部诊断仍可访问
- **WHEN** 具备 operator/model_admin/sys_admin 角色的用户直接访问 `/ops`
- **THEN** 页面以"内部诊断"形态可访问，不再以"运维工作台"作为业务化主交付

#### Scenario: 只读边界不被削弱
- **WHEN** display_readonly 下触发 retry/cancel
- **THEN** 仍返回 `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`，不发 Slurm 控制请求，不写终态

### Requirement: 前端角色来源使用 runtime config

前端判定运维入口显隐 MUST 使用后端 `GET /api/v1/runtime/config` 的 `service_role`/`display_readonly`，MUST NOT 依赖编译期硬编码角色假设。

#### Scenario: 角色由 runtime config 驱动
- **WHEN** runtime config 报告 `service_role=display_readonly`
- **THEN** 前端据此隐藏运维入口，不依赖 build-time 角色常量
