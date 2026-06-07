## ADDED Requirements

### Requirement: latest-product 以独立 supplemental 字段标注洪水重现期可用性

latest-product 响应 SHALL 在 `availability` 下提供独立的洪水重现期可用性字段 `return_period_status`（取值 `ready` / `unavailable`），unavailable 时携带 reason code `RETURN_PERIOD_RESULT_UNAVAILABLE`。该字段 MUST 为 supplemental：MUST NOT 进入决定产品是否 ready/是否返回的 blocking `unavailable_reasons` 集合，MUST NOT 改变产品 ready 判定或导致产品不被返回。

#### Scenario: 有流量无洪频基线时产品仍可返回
- **WHEN** latest-product 选中的 run 有 q_down 河段输出，但 `flood.return_period_result` 无非空 peak 行
- **THEN** 产品仍按现有 ready 判定正常返回（不掉 ready、不返回 404），且 `availability.return_period_status = unavailable` 并带 reason `RETURN_PERIOD_RESULT_UNAVAILABLE`

#### Scenario: 有重现期产品时标为 ready
- **WHEN** 选中的 run 在 `flood.return_period_result` 有非空 peak return-period 行
- **THEN** `availability.return_period_status = ready`

#### Scenario: 不破坏既有 ready 与 blocking reasons 契约
- **WHEN** 现有消费者依据 `availability.ready` 与既有 `unavailable_reasons` 判定产品可用性
- **THEN** 这两项的取值与本变更前一致，return-period 状态仅以新增独立字段体现（OpenAPI schema 扩展为新增字段，不修改既有字段语义）

### Requirement: return-period 可用性口径与 best-available 一致（非空 peak 行）

return-period 可用性判定 MUST 与 best-available / `/runs` 使用同一口径——`flood.return_period_result` 中**非空 peak 行**（`flood_return_period_rows > 0`）的存在性，MUST NOT 改用任意 timestep 行计数。

#### Scenario: 仅有非 peak 行不算可用
- **WHEN** 某 run 在 `flood.return_period_result` 仅有非 peak（如逐时 timestep）的 return-period 行、无非空 peak 行
- **THEN** `return_period_status = unavailable`，与 best-available 对该 run 的判定一致

#### Scenario: 跨接口口径一致
- **WHEN** 同一 run 在 best-available 被判 return-period unavailable
- **THEN** latest-product 的 `return_period_status` 也为 unavailable
