// overview store 的 barrel（#2102 拆分）：实现分到下列同目录子模块，本文件只做导出聚合。
// 导出名与签名与拆分前逐字相同；子模块之间直接互相 import，不经过本文件。
// `overviewDataStore.ts` 同时持有四个模块级 `let`（nonce / active key / rederive /
// cacheGeneration）、cache、fetchers 与 `create<>()` 本体：ES 模块的 import binding 只读，
// 赋值语句必须与声明同模块，而 fetchers 依赖 `cached`、`cached` 依赖 `cacheGeneration`，
// 故三者只能同处一个模块，否则会成环。显式列表而非 `export *`：内部 helper 不得进入公共面。

export { nationalConcreteSource } from '@/stores/overviewDataNationalDischarge'
export { overviewSnapshotMatchesQuery, overviewSnapshotMetadataMatchesQuery } from '@/stores/overviewDataRequestScope'
export { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewDataStore'
export {
  m11SourceCycleKey,
  type DischargeCycles,
  type DischargeCyclesState,
  type M11OverviewRequestScope,
  type M11SnapshotRequestScope,
  type OverviewBootstrapSnapshot,
  type OverviewDataSnapshot,
  type PrecipIndex,
  type PrecipIndexState,
  type ValidTimesState,
} from '@/stores/overviewDataTypes'
