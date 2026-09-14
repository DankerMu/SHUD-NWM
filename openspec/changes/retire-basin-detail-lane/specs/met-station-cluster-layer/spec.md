## MODIFIED Requirements

### Requirement: 代站数据按选中流域严格身份取数，分页至 client cap 且诚实标注 truncation

代站 GeoJSON 数据 SHALL 经独立 `stores/stationLayerData.ts`（薄 store 复用 `loadHydroMetBootstrap`→`fetchHydroMetStations`）按当前选中流域 latest-product 的严格身份（model_id/basin_version_id/source/cycle_time）加载，MUST NOT 污染 `overviewData` store。由于单次接口 `limit ≤ 500` 而流域站点数可超过 500（如 Heihe 1709），store MUST 分页（offset 翻页）拉取至一个明确的 client cap，并 MUST 暴露 `total`/`loaded`/`truncated` 供 UI 与 receipt 诚实标注（达到 cap 未取全时显式标 truncated，不得让"看似完整"的图层掩盖缺失）。源为 `best`/`compare` 时 MUST 先解析为具体 GFS/IFS 再取数。

#### Scenario: 流域站点超 500 时分页取至 cap 并标注
- **WHEN** 选中 Heihe（1709 站）、源解析为 GFS
- **THEN** store 分页拉取至 client cap，暴露 `total=1709`/`loaded`/`truncated`；若未取全，UI 与 receipt MUST 显式标注 truncated

#### Scenario: 按选中流域身份加载代站
- **WHEN** 选中流域为 Qhh（386 站）、源解析为 GFS
- **THEN** 代站 store 以 Qhh GFS latest-product 身份取全部 386 站（≤cap，`truncated=false`），渲染该流域代站

#### Scenario: 源未解析时不取数
- **WHEN** 源为 best 且尚未解析为具体 GFS/IFS
- **THEN** 代站图层不发起以 `best` 为源的取数，等待 `resolvedSource`

#### Scenario: 全国总览无可用流域版本时开启代站图层的 honest 空态
- **WHEN** 处于全国总览、没有任何可解析的流域版本上下文，且用户开启"气象代站"图层（如经 `/meteorology`→`/?layer=met-stations` 进入）
- **THEN** MUST NOT 以无流域身份误打接口或拉全量，显示"暂无可用流域版本以加载气象代站"类 honest 空态；该判定不依赖任何 `basinId` URL 键（流域详情模式已退役，#2109 裁决 B）
