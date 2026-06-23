## MODIFIED Requirements

### Requirement: 旧展示路由收敛/重定向到单页

The frontend SHALL redirect legacy display routes (`/overview`, `/hydro-met`,
`/meteorology`, `/forecast`, `/flood-alerts`, `/basins/:basinId`, and
`/segments/:segmentId`) to the single page `/`. Redirects MUST use `replace` so
browser history is not polluted, MUST preserve the original search query, and
MUST append semantic mapping parameters. `/meteorology` SHALL append
`metStations=1` instead of the retired primary-layer state `layer=met-stations`;
`/flood-alerts` SHALL append
`layer=flood-return-period`; `/basins/:basinId` SHALL append
`basinId=:basinId`; `/segments/:segmentId` SHALL append
`segmentId=:segmentId`. When the original search already contains the same key,
the original search value MUST win.

#### Scenario: 旧展示路由重定向

- **WHEN** a user visits `/overview`, `/hydro-met`, or `/forecast`
- **THEN** the browser URL lands on `/` with `replace`
- **AND** the single-page map renders.

#### Scenario: 带语义的重定向保留 query

- **WHEN** a user visits `/meteorology`, `/flood-alerts`,
  `/basins/basins_qhh`, or `/segments/seg_001`
- **THEN** the redirect targets `/` with semantic query state equivalent to
  `?metStations=1`, `?layer=flood-return-period`,
  `?basinId=basins_qhh`, and `?segmentId=seg_001` respectively.

#### Scenario: 深链原始 search 不丢失

- **WHEN** a user visits a deep link with state such as
  `/meteorology?source=IFS&validTime=2026-06-05T18:00:00Z`
- **THEN** the redirect target preserves the original `source` and `validTime`
  parameters
- **AND** it appends `metStations=1`
- **AND** it MUST NOT append `layer=met-stations`.

#### Scenario: 原始 layer 与气象入口共存

- **WHEN** a user visits `/meteorology?layer=flood-return-period`
- **THEN** the redirect target preserves `layer=flood-return-period`
- **AND** it enables `metStations=1` so the station overlay appears above the
  requested hydrology layer.

#### Scenario: 缺 basin 上下文的 segment 深链 honest 处理

- **WHEN** a user visits `/segments/:segmentId` but the frontend cannot resolve
  basin context from the query
- **THEN** the redirect lands on `/?segmentId=:segmentId`
- **AND** the single page shows an honest state requiring basin context
- **AND** it MUST NOT fabricate a selected segment or choose an arbitrary basin.
