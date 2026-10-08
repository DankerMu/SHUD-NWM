## MODIFIED Requirements

### Requirement: 代站数据按选中流域严格身份取数，分页至 client cap 且诚实标注 truncation

代站 GeoJSON 数据 SHALL 经独立 `stores/stationLayerData.ts` 按全国总览当前可见流域上下文加载（流域详情模式已退役，#2109 裁决 B），MUST NOT 污染 `overviewData` store。每个流域上下文的站点清单 SHALL 使用站点接口支持的 basin/model 范围（`basin_version_id`；源已解析为具体 GFS/IFS 时取该源 latest-product 的 `basin_version_id` 与 `model_id`），MUST NOT 宣称站点清单接口按 source/cycle 过滤。由于单次接口 `limit ≤ 500` 而流域站点数可超过 500（如 `basins_hlj_vbasins` 1314），store MUST 分页（offset 翻页）拉取至一个明确的 client cap，并 MUST 暴露 `total`/`loaded`/`truncated` 供 UI 与 receipt 诚实标注（达到 cap 未取全时显式标 truncated，不得让"看似完整"的图层掩盖缺失）。源已解析但该流域的 latest-product 查询失败（含无该源产品）时 SHALL 回退为仅按 `basin_version_id` 取清单，不算失败。单个流域上下文的站点请求失败 MUST NOT 使其余流域的代站消失：store SHALL 继续加载其余上下文，在 `failedBasinIds` 中列出失败的流域并标 `truncated`；全部上下文失败时 SHALL 以错误态呈现，MUST NOT 呈现为已加载的空图层。点击代站后的 station-series 曲线取数仍要求 source/cycle 严格身份：源为 `best`/`compare` 时 MUST 先解析为具体 GFS/IFS 再取数。

#### Scenario: 流域站点超 500 时分页取至 cap 并标注
- **WHEN** a visible basin has more station rows than the backend single-request limit
- **THEN** the station store MUST request additional pages until the documented client cap or total is reached
- **AND** if `loaded < total`, the UI/receipt MUST mark the station overlay as truncated

#### Scenario: 按选中流域身份加载代站
- **WHEN** the user is on the national overview with visible QHH and Heihe basin contexts
- **AND** the station overlay is enabled
- **THEN** station loading MUST request stations for those visible basin contexts
- **AND** the map MUST be able to display QHH and Heihe station features together, subject to the client cap and honest truncation state

#### Scenario: 源未解析时不取数
- **WHEN** the selected source is `best` or `compare` and no concrete GFS/IFS source has been resolved
- **THEN** clicking a station MUST NOT request station-series data with `best` or `compare` as the source id
- **AND** the station curve window MUST show an honest waiting or unavailable state until a concrete source is available

#### Scenario: 全国总览无可用流域版本时开启代站图层的 honest 空态
- **WHEN** 处于全国总览、没有任何可解析的流域版本上下文，且用户开启"气象代站"图层（如经 `/meteorology`→`/?metStations=1` 进入）
- **THEN** MUST NOT 以无流域身份误打接口或拉全量，显示"暂无可用流域版本以加载气象代站"类 honest 空态；该判定不依赖任何 `basinId` URL 键（流域详情模式已退役，#2109 裁决 B）

#### Scenario: 单个流域取数失败时其余流域仍渲染
- **WHEN** 多个可见流域上下文中有一个的站点请求失败，其余成功
- **THEN** 成功流域的代站 MUST 照常渲染，失败流域的 id MUST 出现在 `failedBasinIds` 与状态文案中，图层 MUST 标为 truncated

#### Scenario: 全部流域取数失败
- **WHEN** 所有被请求的流域上下文都失败
- **THEN** 图层 MUST 呈现错误态，MUST NOT 呈现为已加载的空图层

#### Scenario: 流域无该源 latest-product
- **WHEN** 源已解析为 GFS/IFS，而某可见流域的 latest-product 查询失败（如 404 无可用产品）
- **THEN** 该流域 MUST 回退为按 `basin_version_id` 取站点清单，MUST NOT 计入 `failedBasinIds`
