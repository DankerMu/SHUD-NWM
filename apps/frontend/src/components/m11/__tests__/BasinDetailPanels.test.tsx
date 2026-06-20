import { describe, expect, it } from 'vitest'

import { basinDetailToOverviewBasin } from '@/components/m11/BasinDetailPanels'
import {
  createEmptyBasinDetail,
  createFreshnessMetadata,
  createSourceScenarioSelection,
  emptyWarningCounts,
  type BasinDetail,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState } from '@/lib/m11/queryState'

// PR 4/7 task 4.5 (b)：BasinDetailPanels 在 warningDistribution === undefined 时必须传递 pending
// 占位（warningCounts === undefined），而非把 undefined 错误地填成"全 0 警告"误导前端 UI。
// spec: "Default overview bootstrap omits ranking" / "BasinDetailPanels MUST tolerate an empty /
// `pending` `warningDistribution` until lazy ranking settles, without rendering a misleading
// 'all zero warnings' state"。

function buildDetail(overrides: Partial<BasinDetail>): BasinDetail {
  return { ...createEmptyBasinDetail('yangtze', defaultM11QueryState), ...overrides }
}

describe('BasinDetailPanels.basinDetailToOverviewBasin warningCounts pending degradation', () => {
  it('forwards undefined warningDistribution as undefined warningCounts (pending / not-loaded placeholder)', () => {
    const detail = buildDetail({
      basinId: 'yangtze',
      displayName: 'Yangtze',
      warningDistribution: undefined,
    })

    const overviewBasin = basinDetailToOverviewBasin(detail)

    // 关键合同：pending 态必须保留为 undefined；任何"为零填充"都会让消费 UI 错把 pending 当 ready。
    expect(overviewBasin.warningCounts).toBeUndefined()
  })

  it('forwards an explicitly-loaded all-zero warningDistribution as a zero record (real all-zero is not pending)', () => {
    const detail = buildDetail({
      basinId: 'yangtze',
      displayName: 'Yangtze',
      // ranking 已 settle 但确实全 0 → 显式的 record，不应误降级为 undefined。
      warningDistribution: { ...emptyWarningCounts },
    })

    const overviewBasin = basinDetailToOverviewBasin(detail)

    expect(overviewBasin.warningCounts).toBeDefined()
    expect(overviewBasin.warningCounts).toEqual(emptyWarningCounts)
  })

  it('createEmptyBasinDetail starts with undefined warningDistribution (pre-ranking placeholder)', () => {
    const empty = createEmptyBasinDetail('yangtze', {
      ...defaultM11QueryState,
      source: 'gfs',
      cycle: null,
      validTime: null,
    })

    // 空详情即 pending 起点：warningDistribution 必须为 undefined（不是 0 填充）。
    expect(empty.warningDistribution).toBeUndefined()
    // 与之对照：sourceSelection 仍正常构造，确认 warningDistribution 的 undefined 是设计选择而非
    // 整体未初始化。
    expect(empty.sourceSelection).toEqual(
      createSourceScenarioSelection({ source: 'gfs', cycle: null, validTime: null }),
    )
    expect(empty.latestRun).toEqual(
      createFreshnessMetadata({
        source: empty.sourceSelection.resolvedSource,
        unavailableReason: 'No basin data loaded.',
      }),
    )
  })
})
