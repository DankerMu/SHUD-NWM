## ADDED Requirements

### Requirement: latest-product 按 basin_id 参数化

latest-product 查询 SHALL 接受 `basin_id` 维度并据此选取产品。当未提供 `basin_id` 时 MUST 默认 `basins_qhh` 以保持 `/api/v1/mvp/qhh/latest-product` 旧路径与 M22 cross-plane 调用向后兼容。

#### Scenario: 按流域取得对应产品
- **WHEN** 调用方以 `basin_id=basins_heihe` 请求 latest-product，且 heihe 有 published run
- **THEN** 响应返回 heihe 的产品身份（其 `basin_version_id`/`model_id`/`run_id`），不返回 qhh 的产品

#### Scenario: 缺省默认 QHH 向后兼容
- **WHEN** 调用方不提供 `basin_id` 请求 latest-product
- **THEN** 响应等价于本变更前的 QHH latest-product 行为

#### Scenario: M22 cross-plane 旧调用不破
- **WHEN** 旧 cross-plane 消费者不带 `basin_id`、携带完整 strict identity（`source`/`cycle_time`/`run_id`/`model_id`）调用 `/api/v1/mvp/qhh/latest-product`
- **THEN** 返回与本变更前一致的 QHH 产品 identity 与 availability 结构，消费者无需修改请求参数或响应解析逻辑

#### Scenario: 目标流域无产品返回诚实 unavailable
- **WHEN** 调用方以某流域 `basin_id` 请求，但该流域无任何 ready run
- **THEN** 响应为该流域的 unavailable，且 MUST NOT 串用其他流域的产品冒充

### Requirement: 移除 QHH 流域硬编码

latest-product 的 SQL 过滤与错误/不可用响应 MUST 依据请求的 `basin_id`，MUST NOT 在查询条件或响应体中写死 `basins_qhh`。

#### Scenario: 查询与响应不写死 qhh
- **WHEN** 以 `basin_id=basins_heihe` 请求且无产品
- **THEN** 返回的不可用响应中的流域标识为 `basins_heihe`，不出现被硬编码的 `basins_qhh`

### Requirement: 多流域下保持 strict identity

提供 strict identity（`source`/`cycle_time`/`run_id`/`model_id`）时，latest-product MUST 在指定 `basin_id` 内精确匹配，且 MUST NOT 回退到 historical latest。

#### Scenario: basin 与 strict identity 联合精确匹配
- **WHEN** 调用方提供 `basin_id` 与完整 strict identity，但该 identity 在该流域不存在
- **THEN** 返回 unavailable，且不返回该流域的任何其他历史 run
