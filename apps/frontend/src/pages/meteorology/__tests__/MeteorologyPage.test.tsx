import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { MeteorologyPage, meteorologyDependencyDecision } from '@/pages/meteorology/MeteorologyPage'
import { buildMeteorologyGridViewModel, buildStationInventoryViewModel } from '@/lib/meteorology/viewModels'
import {
  getMeteorologyGridContract,
  getMeteorologyStationSeries,
  hasMinimumMeteorologyContracts,
  meteorologyBbox,
  projectLonLatToPercent,
} from '@/lib/meteorology/contracts'
import { parseMeteorologyQueryState, serializeMeteorologyQueryState } from '@/lib/meteorology/queryState'

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => <pre data-testid="mock-echarts-option">{JSON.stringify(option)}</pre>,
}))

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

function renderMeteorology(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/meteorology" element={<MeteorologyPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('meteorology query state', () => {
  it('normalizes unsupported tab and preserves supported grid state', () => {
    const state = parseMeteorologyQueryState(
      'tab=bad&variable=TEMP&source=GFS&validTime=2026-05-18T06:00:00Z&opacity=110&contours=1',
    )

    expect(state.tab).toBe('grid')
    expect(state.variable).toBe('TEMP')
    expect(state.source).toBe('GFS')
    expect(state.validTime).toBe('2026-05-18T06:00:00.000Z')
    expect(state.gridQueryLon).toBeNull()
    expect(state.opacity).toBe(100)
    expect(serializeMeteorologyQueryState({ ...state, tab: 'stations', search: 'HMT' })).toContain('tab=stations')
  })

  it('keeps overlong search evidence reachable while bounding the effective value', () => {
    const state = parseMeteorologyQueryState(`tab=stations&search=${'x'.repeat(81)}`)

    expect(state.search).toHaveLength(80)
    expect(state.searchValidationReason).toContain('超过 80 字符')
  })
})

describe('meteorology grid contract view model', () => {
  it('projects lon/lat to clamped bbox percentages deterministically', () => {
    expect(projectLonLatToPercent(meteorologyBbox.minLon, meteorologyBbox.maxLat)).toEqual({ left: 0, top: 0, clamped: false })
    expect(projectLonLatToPercent(meteorologyBbox.maxLon, meteorologyBbox.minLat)).toEqual({ left: 100, top: 100, clamped: false })
    expect(projectLonLatToPercent(200, 0)).toEqual({ left: 100, top: 100, clamped: true })
  })

  it('displays all required variables and sources without generated values', () => {
    for (const variable of ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'] as const) {
      for (const source of ['GFS', 'IFS', 'ERA5', 'CLDAS', 'Best Available'] as const) {
        const contract = getMeteorologyGridContract(variable, source)
        expect(contract.unit).toBeTruthy()
        expect(contract.bbox.minLon).toBe(73)
        expect(contract.spatialResolution).toBeTruthy()
        expect(contract.nativeTimeResolution).toBeTruthy()
        expect(contract.restrictedReason ?? contract.unavailableReason).toBeTruthy()
      }
    }
  })

  it('corrects stale valid times and reports unsupported comparisons', () => {
    const model = buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'IFS',
      validTime: '2026-05-18T06:00:00.000Z',
      gridQueryLon: null,
      gridQueryLat: null,
      compareSource: 'GFS',
    })

    expect(model.correctedValidTime).toBe('2026-05-18T12:00:00.000Z')
    expect(model.comparisonStatus).toContain('具备合同可比性')

    const unsupported = buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'GFS',
      validTime: '2026-05-18T06:00:00.000Z',
      gridQueryLon: null,
      gridQueryLat: null,
      compareSource: 'CLDAS',
    })
    expect(unsupported.comparisonStatus).toContain('不支持')
  })

  it('keeps CLDAS restricted and disables timeline/query states', () => {
    const model = buildMeteorologyGridViewModel({
      variable: 'TEMP',
      source: 'CLDAS',
      validTime: '2026-05-18T06:00:00.000Z',
      gridQueryLon: null,
      gridQueryLat: null,
      compareSource: null,
    })

    expect(model.correctedValidTime).toBeNull()
    expect(model.timelineDisabledReason).toContain('CLDAS')
    expect(model.cellPopup).toBeNull()
  })

  it('creates grid cell popup only from query coordinates and reports bounds/restriction state', () => {
    expect(buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'GFS',
      validTime: '2026-05-18T06:00:00.000Z',
      gridQueryLon: null,
      gridQueryLat: null,
      compareSource: null,
    }).cellPopup).toBeNull()

    const clicked = buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'GFS',
      validTime: '2026-05-18T06:00:00.000Z',
      gridQueryLon: 114.35,
      gridQueryLat: 30.62,
      compareSource: null,
    })
    expect(clicked.cellPopup?.reason).toContain('不生成替代数值')
    expect(clicked.cellPopup?.left).toBeCloseTo(projectLonLatToPercent(114.35, 30.62).left)

    const outOfBounds = buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'GFS',
      validTime: '2026-05-18T06:00:00.000Z',
      gridQueryLon: 140,
      gridQueryLat: 60,
      compareSource: null,
    })
    expect(outOfBounds.cellPopup?.reason).toContain('超出合同 bbox')

    const restricted = buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'CLDAS',
      validTime: null,
      gridQueryLon: 114.35,
      gridQueryLat: 30.62,
      compareSource: null,
    })
    expect(restricted.cellPopup?.reason).toContain('CLDAS 数据权限尚未开通')
  })
})

describe('MeteorologyPage grid tab', () => {
  it('restores grid tab state and visibly corrects stale valid time', async () => {
    renderMeteorology('/meteorology?tab=grid&variable=TEMP&source=GFS&validTime=2020-01-01T00:00:00.000Z&opacity=65')

    expect(await screen.findByRole('tab', { selected: true, name: /空间栅格/ })).toBeInTheDocument()
    expect(screen.getByText('TEMP')).toBeInTheDocument()
    expect(screen.getByText(/实时栅格瓦片服务尚未接入/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText(/2026-05-18T18:00:00.000Z/).length).toBeGreaterThan(0))
    expect(screen.queryByTestId('grid-cell-popup')).not.toBeInTheDocument()
  })

  it('opens contract grid query popup after a map click without fabricating values', async () => {
    const user = userEvent.setup()
    renderMeteorology('/meteorology?tab=grid&variable=PRCP&source=GFS&validTime=2026-05-18T06:00:00.000Z')

    expect(screen.queryByTestId('grid-cell-popup')).not.toBeInTheDocument()
    await user.click(await screen.findByTestId('meteorology-grid-map'))
    expect(await screen.findByTestId('grid-cell-popup')).toHaveTextContent('UI 不生成替代数值')
    expect(screen.getByTestId('grid-cell-popup')).toHaveTextContent('PRCP / mm/day')
  })

  it('renders CLDAS restricted state and clears stale grid popup', async () => {
    renderMeteorology('/meteorology?tab=grid&source=CLDAS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z')

    expect(await screen.findByTestId('cldas-restricted')).toHaveTextContent('CLDAS 数据权限尚未开通')
    expect(screen.queryByTestId('grid-cell-popup')).not.toBeInTheDocument()
    expect(screen.getByTestId('grid-timeline')).toHaveTextContent('CLDAS 数据权限尚未开通')
  })

  it('shows scoped unsupported comparison and bounded area-stat states', async () => {
    renderMeteorology('/meteorology?tab=grid&source=GFS&variable=PRCP&compareSource=CLDAS&validTime=2026-05-18T06:00:00.000Z')

    expect(await screen.findByTestId('comparison-status')).toHaveTextContent('不支持')
    expect(screen.getByTestId('area-stats-status')).toHaveTextContent('请求上限')
  })
})

describe('MeteorologyPage station tab', () => {
  it('restores station filters and renders bounded inventory, popup, QC charts, and adjacent stations', async () => {
    renderMeteorology('/meteorology?tab=stations&basin=yangtze&search=HMT-Y2&sort=completeness&stationId=HMT-Y2-0237')

    expect(await screen.findByRole('tab', { selected: true, name: /气象代站/ })).toBeInTheDocument()
    expect(screen.getByTestId('station-popup')).toHaveTextContent('HMT-Y2-0237')
    expect(screen.getByTestId('adjacent-stations')).toHaveTextContent('HMT-Y2-0236')
    expect(screen.getByTestId('forcing-charts')).toHaveTextContent('PRCP')
    expect(screen.getByTestId('forcing-Rn-unavailable')).toHaveTextContent('Rn')
    expect(screen.getByTestId('forcing-series-truncated')).toHaveTextContent('样本上限')
    expect(screen.queryByTestId('mock-echarts-option')).not.toBeInTheDocument()
  })

  it('keeps station marker position stable across sort changes and syncs selected detail', async () => {
    const user = userEvent.setup()
    renderMeteorology('/meteorology?tab=stations&basin=yangtze&sort=latest&stationId=HMT-Y2-0237')

    const marker = await screen.findByTestId('station-marker-HMT-Y2-0237')
    const initialLeft = marker.style.left
    const initialTop = marker.style.top
    await user.selectOptions(screen.getByLabelText('排序', { selector: 'select' }), 'station_id')

    const sortedMarker = await screen.findByTestId('station-marker-HMT-Y2-0237')
    expect(sortedMarker.style.left).toBe(initialLeft)
    expect(sortedMarker.style.top).toBe(initialTop)
    expect(screen.getByTestId('station-popup')).toHaveTextContent('HMT-Y2-0237')
  })

  it('renders reachable station search validation and inventory truncation states', async () => {
    const validationRender = renderMeteorology(`/meteorology?tab=stations&search=${'HMT'.repeat(30)}`)

    expect(await screen.findByText(/超过 80 字符/)).toBeInTheDocument()
    expect(screen.getByTestId('station-empty')).toHaveTextContent('搜索无结果')

    validationRender.unmount()
    renderMeteorology('/meteorology?tab=stations')
    expect(await screen.findByTestId('station-inventory-truncated')).toHaveTextContent('每页 2 条')
  })

  it('shows no-station empty state without fake rows', async () => {
    renderMeteorology('/meteorology?tab=stations&search=does-not-exist')

    expect(await screen.findByTestId('station-empty')).toHaveTextContent('搜索无结果')
    expect(screen.queryByTestId('station-popup')).not.toBeInTheDocument()
  })

  it('clears stale station detail when basin filter excludes selected station', async () => {
    const user = userEvent.setup()
    renderMeteorology('/meteorology?tab=stations&stationId=HMT-Y2-0236')

    expect(await screen.findByTestId('station-popup')).toHaveTextContent('HMT-Y2-0236')
    await user.selectOptions(screen.getByLabelText('流域', { selector: 'select' }), 'hanjiang')

    await waitFor(() => expect(screen.queryByText('武汉代站')).not.toBeInTheDocument())
    expect(screen.getByTestId('station-popup')).toHaveTextContent('HMT-HAN-0081')
    expect(screen.getByTestId('forcing-unavailable')).toHaveTextContent('所选时间范围没有可用 forcing series')
  })
})

describe('station resource view model', () => {
  it('bounds search and reports unavailable forcing explicitly', () => {
    const model = buildStationInventoryViewModel({
      basin: 'hanjiang',
      search: null,
      searchValidationReason: null,
      sort: 'latest',
      stationId: 'HMT-HAN-0081',
    })

    expect(model.rows).toHaveLength(1)
    expect(model.selectedSeries?.sampleLimit).toBe(48)
    expect(model.selectedSeries?.truncated).toBe(false)
    expect(model.selectedSeries?.variables.some((variable) => variable.unavailableReason)).toBe(true)
    expect(meteorologyDependencyDecision).toContain('No dependency change')
  })

  it('exposes all required station variables including Rn without synthetic points', () => {
    const series = getMeteorologyStationSeries('HMT-Y2-0237')

    expect(series?.variables.map((variable) => variable.variable)).toEqual(['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'])
    expect(series?.truncated).toBe(true)
    expect(series?.variables.every((variable) => variable.valueStatus === 'unavailable')).toBe(true)
    expect(series?.variables.every((variable) => variable.points.length === 0)).toBe(true)
  })

  it('documents that bundled fixture contracts make meteorology navigation available', () => {
    expect(hasMinimumMeteorologyContracts()).toBe(true)
  })
})
