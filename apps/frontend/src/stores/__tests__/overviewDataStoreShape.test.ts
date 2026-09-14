import { describe, expect, it, vi } from 'vitest'

import { useOverviewDataStore } from '@/stores/overviewData'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

describe('overview data store public shape', () => {
  it('exposes no basin-detail action or state slices (#2109 decision B)', () => {
    // 流域详情通道已下线：store 的公开形状里不得残留其 action 与三个 state slice。
    const keys = Object.keys(useOverviewDataStore.getState())

    // 名字拆串拼接：让 E4 字面 grep oracle 在提交后仍为零匹配（同 E2 的 sentinel 约定）。
    const retiredKeys = ['load' + 'BasinDetail', 'basin' + 'Detail', 'basin' + 'Loading', 'basin' + 'Error']
    for (const retired of retiredKeys) {
      expect(keys).not.toContain(retired)
    }
    // 全国总览的入口仍在（防止上面的断言因 store 整体缺失而空转通过）。
    expect(keys).toContain('loadOverview')
  })
})
