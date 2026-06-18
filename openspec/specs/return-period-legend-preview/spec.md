# return-period-legend-preview Specification

## Purpose
TBD - created by archiving change m25-multibasin-frontend-production. Update Purpose after archive.
## Requirements
### Requirement: 洪水重现期区块诚实三态

`/hydro-met` 内的洪水重现期区块 SHALL 根据 latest-product 的 return-period 可用性渲染状态。当为 unavailable 时 MUST 显示"暂未发布正式产品"提示且不渲染任何产品数据；同时 SHALL 展示静态分级图例（2y/5y/10y/20y/50y/100y）作为分级说明。

#### Scenario: 不可用时显示占位与图例
- **WHEN** latest-product 返回 `RETURN_PERIOD_RESULT_UNAVAILABLE`
- **THEN** 区块显示"暂未发布正式产品"，展示静态分级图例，且不绘制任何河段重现期产品

#### Scenario: 图例为静态领域知识
- **WHEN** 用户查看洪水重现期区块
- **THEN** 分级图例（等级与颜色标签）作为静态说明展示，不依赖任何产品数据接口

### Requirement: 禁止伪造洪水重现期产品

洪水重现期区块 MUST NOT 在无真实产品数据时展示"正式产品已发布"类文案或渲染假河段数据，MUST NOT 调用不存在的 preview/status 接口。

#### Scenario: 无数据不出现已发布文案
- **WHEN** 当前无真实 return-period 产品
- **THEN** 页面任何位置不出现"正式洪水重现期产品已发布"或等义文案

#### Scenario: 不引入造假数据通路
- **WHEN** 渲染洪水重现期区块
- **THEN** 前端不请求 `flood-return-period/preview` 或 `flood-return-period/status` 等本变更明确排除的接口，不加载 preview 假河段 fixture

