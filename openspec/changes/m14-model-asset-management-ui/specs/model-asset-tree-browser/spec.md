## ADDED Requirements

### Requirement: Model asset tree browser
The page SHALL present a searchable basin/model tree with active model highlighting and filters.

#### Scenario: Models loaded
WHEN model list API returns Basins-backed models
THEN tree groups by basin and model, highlights active models, and preserves selected model in URL/state

#### Scenario: Empty registry
WHEN model list is empty
THEN page displays `暂无模型资产`

#### Scenario: Search no results
WHEN search/filter state excludes all loaded models
THEN page displays `无匹配模型`

#### Scenario: Stale selection
WHEN search/filter changes exclude the currently selected model
THEN detail state is cleared or marked out-of-filter without displaying stale model detail
