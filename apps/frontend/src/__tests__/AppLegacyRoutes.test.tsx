import { render, screen } from '@testing-library/react'
import { Suspense } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { AppRoutes } from '@/App'

// 桩掉真页面：本文件只钉**路由声明**把旧链接落到哪个 location，不需要页面的 API harness。
// `App` 按 `module.OverviewPage` 取具名导出，故桩必须以同名导出提供。
vi.mock('@/pages/OverviewPage', () => ({
  OverviewPage: function OverviewPageLocationStub() {
    const location = useLocation()
    return (
      <div data-testid="overview-location" data-pathname={location.pathname} data-search={location.search} />
    )
  },
}))

async function landedLocationFor(entry: string) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Suspense fallback={null}>
        <AppRoutes />
      </Suspense>
    </MemoryRouter>,
  )
  const stub = await screen.findByTestId('overview-location')
  return {
    pathname: stub.getAttribute('data-pathname'),
    search: new URLSearchParams(stub.getAttribute('data-search') ?? ''),
  }
}

describe('AppRoutes legacy basin link', () => {
  it('lands /basins/:basinId on the national overview, dropping the path parameter and keeping the query', async () => {
    // #2109 裁决 B：流域详情通道已下线。旧书签仍须落到 `/`，路径参数不再映射成 `basinId` 查询键，
    // 其余 query（source / validTime）原样保留。
    const { pathname, search } = await landedLocationFor(
      '/basins/basins_qhh?source=ifs&validTime=2026-05-18T06:00:00Z',
    )

    expect(pathname).toBe('/')
    expect(search.get('source')).toBe('ifs')
    expect(search.get('validTime')).toBe('2026-05-18T06:00:00Z')
    expect(search.has('basinId')).toBe(false)
  })
})
