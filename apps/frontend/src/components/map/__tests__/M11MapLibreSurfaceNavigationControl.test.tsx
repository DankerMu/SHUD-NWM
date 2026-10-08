import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import { MOBILE_FORM_QUERY } from '@/hooks/useMobileForm'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import { installMaplibreStubMap } from '@/test/maplibreStub'

/**
 * 缩放 / 指北控件按形态渲染（openspec mobile-responsive-display task 3.1，design.md D7）。
 *
 * 共享的 `MaplibreControlStub` 返回 `null` 且与 `ScaleControl` 共用：照它写“渲染 / 不渲染”，
 * 两个分支的 DOM 完全相同，是空断言。这里给 `NavigationControl` 单独装一个带 testid、
 * 并把收到的 props 透出来的桩；`ScaleControl` 也用自己的 testid，作为“兄弟控件仍在”的对照。
 */
vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
  return {
    default: MaplibreMapStub,
    Map: MaplibreMapStub,
    NavigationControl: ({ position, visualizePitch }: { position?: string; visualizePitch?: boolean }) => (
      <div
        data-testid="mock-navigation-control"
        data-position={position}
        data-visualize-pitch={String(visualizePitch)}
      />
    ),
    ScaleControl: () => <div data-testid="mock-scale-control" />,
    Source: MaplibreSourceStub,
    Layer: MaplibreLayerStub,
    Marker: MaplibreMarkerStub,
  }
})

/** 只让移动形态那条查询命中的 `matchMedia` 桩；其余查询（含矮视口横屏）不命中。 */
function installMobileFormMatchMedia() {
  window.matchMedia = ((query: string) =>
    ({
      matches: query === MOBILE_FORM_QUERY,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
    }) as unknown as MediaQueryList) as typeof window.matchMedia
}

function renderSurface() {
  return render(<M11MapLibreSurface state={defaultM11QueryState} layers={[]} loading={false} boundaryLoading={false} />)
}

describe('M11MapLibreSurface navigation control by viewport form', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
    installMaplibreStubMap({
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn(),
      project: vi.fn(() => ({ x: 0, y: 0 })),
      queryRenderedFeatures: vi.fn(() => []),
      getCanvas: () => ({ style: { cursor: '' } }),
      once: (_event: string, callback: () => void) => {
        queueMicrotask(callback)
      },
    })
  })

  afterEach(() => {
    window.matchMedia = originalMatchMedia
  })

  it('desktop form renders exactly one control at top-right with pitch visualisation', () => {
    // 正对照：测试 setup 的 `matchMedia` 桩恒不命中，即桌面形态。
    expect(window.matchMedia(MOBILE_FORM_QUERY).matches).toBe(false)
    renderSurface()

    const controls = screen.queryAllByTestId('mock-navigation-control')
    expect(controls).toHaveLength(1)
    expect(controls[0].getAttribute('data-position')).toBe('top-right')
    expect(controls[0].getAttribute('data-visualize-pitch')).toBe('true')
    expect(screen.queryAllByTestId('mock-scale-control')).toHaveLength(1)
  })

  it('mobile form renders no navigation control while the sibling scale control stays', () => {
    installMobileFormMatchMedia()
    renderSurface()

    // 地图与兄弟控件都挂上了，“没有缩放控件”才不是空过。
    expect(screen.queryAllByTestId('mock-maplibre-map')).toHaveLength(1)
    expect(screen.queryAllByTestId('mock-scale-control')).toHaveLength(1)
    expect(screen.queryAllByTestId('mock-navigation-control')).toHaveLength(0)
  })
})
