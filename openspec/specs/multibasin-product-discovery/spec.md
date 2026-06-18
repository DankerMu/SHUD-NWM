# multibasin-product-discovery Specification

## Purpose
TBD - created by archiving change m25-multibasin-frontend-production. Update Purpose after archive.
## Requirements
### Requirement: 流域发现仅返回含可展示产品的流域

`GET /api/v1/basins` SHALL 支持可选查询参数 `has_display_product`。当 `has_display_product=true` 时，响应 MUST 仅包含在 `hydro.hydro_run` 中存在处于可展示 ready 状态运行的流域；当参数缺省或为 `false` 时，MUST 保持现有"返回全部已注册流域"的行为不变。

#### Scenario: 仅返回有产品的流域
- **WHEN** 调用方请求 `GET /api/v1/basins?has_display_product=true`，且 `basins_qhh` 有 published run、某注册流域 `basins_empty` 无任何 ready run
- **THEN** 响应包含 `basins_qhh`，且不包含 `basins_empty`

#### Scenario: 缺省参数保持向后兼容
- **WHEN** 调用方请求 `GET /api/v1/basins`（不带 `has_display_product`）
- **THEN** 响应返回全部已注册流域，与本变更前行为一致

#### Scenario: 新流域产出产品后自动出现
- **WHEN** 新流域经 `basins_registry_import` 注册并由 22 节点产出 published run 后，调用 `GET /api/v1/basins?has_display_product=true`
- **THEN** 该新流域出现在响应中，无需修改任何前端或后端代码

### Requirement: 流域发现 ready 判定与 latest-product 一致

`has_display_product` 过滤所用的 run ready 判定 MUST 复用与 latest-product 相同的状态集合 `QHH_LATEST_READY_RUN_STATUSES`（`parsed`/`frequency_done`/`published`），不得引入第二套独立口径。

#### Scenario: 发现口径与可取性一致
- **WHEN** 某流域仅有状态为 `downloading` 的 run（不在 ready 集合）
- **THEN** 该流域不出现在 `has_display_product=true` 的发现结果中，且其 latest-product 也返回 unavailable，二者一致

