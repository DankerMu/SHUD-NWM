import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { MeteorologyPage, meteorologyDependencyDecision } from '@/pages/meteorology/MeteorologyPage'
import { buildMeteorologyGridViewModel, buildStationInventoryViewModel } from '@/lib/meteorology/viewModels'
import { getMeteorologyGridContract } from '@/lib/meteorology/contracts'
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
    expect(state.opacity).toBe(100)
    expect(serializeMeteorologyQueryState({ ...state, tab: 'stations', search: 'HMT' })).toContain('tab=stations')
  })
})

describe('meteorology grid contract view model', () => {
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
      compareSource: 'GFS',
    })

    expect(model.correctedValidTime).toBe('2026-05-18T12:00:00.000Z')
    expect(model.comparisonStatus).toContain('具备合同可比性')

    const unsupported = buildMeteorologyGridViewModel({
      variable: 'PRCP',
      source: 'GFS',
      validTime: '2026-05-18T06:00:00.000Z',
      compareSource: 'CLDAS',
    })
    expect(unsupported.comparisonStatus).toContain('不支持')
  })

  it('keeps CLDAS restricted and disables timeline/query states', () => {
    const model = buildMeteorologyGridViewModel({
      variable: 'TEMP',
      source: 'CLDAS',
      validTime: '2026-05-18T06:00:00.000Z',
      compareSource: null,
    })

    expect(model.correctedValidTime).toBeNull()
    expect(model.timelineDisabledReason).toContain('CLDAS')
    expect(model.cellPopup).toBeNull()
  })
})

describe('MeteorologyPage grid tab', () => {
  it('restores grid tab state and visibly corrects stale valid time', async () => {
    renderMeteorology('/meteorology?tab=grid&variable=TEMP&source=GFS&validTime=2020-01-01T00:00:00.000Z&opacity=65')

    expect(await screen.findByRole('tab', { selected: true, name: /空间栅格/ })).toBeInTheDocument()
    expect(screen.getByText('TEMP')).toBeInTheDocument()
    expect(screen.getByText(/实时栅格瓦片服务尚未接入/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText(/2026-05-18T18:00:00.000Z/).length).toBeGreaterThan(0))
    expect(screen.getByTestId('grid-cell-popup')).toHaveTextContent('UI 不生成替代数值')
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
    expect(screen.getAllByTestId('mock-echarts-option').length).toBeGreaterThan(0)
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
      sort: 'latest',
      stationId: 'HMT-HAN-0081',
    })

    expect(model.rows).toHaveLength(1)
    expect(model.selectedSeries?.sampleLimit).toBe(48)
    expect(model.selectedSeries?.variables.some((variable) => variable.unavailableReason)).toBe(true)
    expect(meteorologyDependencyDecision).toContain('No dependency change')
  })
})
