import { act, render, renderHook, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  CHINA_BOUNDS,
  M11_BASEMAP_UNAVAILABLE_NOTICE,
  M11_MAP_MIN_ZOOM,
  M11MapStatusOverlays,
  m11MapStyles,
  useM11MapCamera,
  useM11MapSourceError,
} from '@/components/map/m11MapRuntime'

function renderStatusOverlays(basinBoundaryOverlayEnabled: boolean) {
  return render(
    <M11MapStatusOverlays
      loading={false}
      boundaryLoading={false}
      basinBoundaryOverlayEnabled={basinBoundaryOverlayEnabled}
      basinCount={1}
      basinFeatureCount={0}
      skippedBasinGeometryCount={0}
      unavailableReason={null}
      selectedSegmentMapState="idle"
      selectedSegmentUnavailableReason={null}
      mapSourceError={null}
    />,
  )
}

describe('M11MapStatusOverlays', () => {
  it('does not report a missing basin boundary when the boundary overlay is intentionally disabled', () => {
    renderStatusOverlays(false)

    expect(screen.queryByTestId('m11-basin-layer-unavailable')).not.toBeInTheDocument()
  })

  it('keeps the boundary unavailable notice for an enabled overlay with no visible features', () => {
    renderStatusOverlays(true)

    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('当前没有可见流域边界。')
  })
})

describe('m11MapStyles (Tianditu via same-origin proxy)', () => {
  it.each(Object.entries(m11MapStyles))('%s basemap has a background fallback and proxied, key-free tile URLs', (_basemap, style) => {
    expect(style.layers[0]).toMatchObject({ id: 'basemap-background', type: 'background' })
    const sources = Object.values(style.sources) as Array<{ type: string; tiles: string[]; maxzoom: number }>
    expect(sources).toHaveLength(2)
    for (const source of sources) {
      expect(source.type).toBe('raster')
      expect(source.tiles).toHaveLength(1)
      expect(source.tiles[0]).toMatch(
        new RegExp(`^${window.location.origin}/api/v1/basemap/tianditu/(vec|cva|img|cia|ter|cta)/\\{z\\}/\\{x\\}/\\{y\\}$`),
      )
      expect(source.tiles[0]).not.toContain('tianditu.gov.cn')
      expect(source.tiles[0]).not.toContain('tk=')
    }
  })

  it('stops requesting tiles above the provider published zoom', () => {
    const maxzoom = (basemap: keyof typeof m11MapStyles) =>
      Object.values(m11MapStyles[basemap].sources).map((source) => (source as { maxzoom: number }).maxzoom)
    expect(maxzoom('terrain')).toEqual([14, 14])
    expect(maxzoom('satellite')).toEqual([18, 18])
    expect(maxzoom('vector')).toEqual([18, 18])
  })
})

describe('useM11MapSourceError', () => {
  const tileError = (sourceId: string) => ({
    sourceId,
    error: { message: `AJAXError: Service Unavailable (503): ${window.location.origin}/api/v1/basemap/tianditu/vec/1/1/0` },
  })

  it('collapses basemap tile failures into one stable notice', () => {
    const { result } = renderHook(() => useM11MapSourceError('k'))

    act(() => result.current.handleMapError(tileError('vec-base')))
    act(() => result.current.handleMapError(tileError('cva-anno')))

    expect(result.current.mapSourceError).toBe(M11_BASEMAP_UNAVAILABLE_NOTICE)
  })

  it('never hides a business-layer error behind the basemap notice', () => {
    const { result } = renderHook(() => useM11MapSourceError('k'))

    act(() => result.current.handleMapError({ sourceId: 'hydro-mvt', error: { message: 'hydro tile failed' } }))
    act(() => result.current.handleMapError(tileError('img-base')))

    expect(result.current.mapSourceError).toBe('hydro tile failed')
  })

  it('lets a business-layer error replace the basemap notice', () => {
    const { result } = renderHook(() => useM11MapSourceError('k'))

    act(() => result.current.handleMapError(tileError('ter-base')))
    act(() => result.current.handleMapError({ sourceId: 'hydro-mvt', error: { message: 'hydro tile failed' } }))

    expect(result.current.mapSourceError).toBe('hydro tile failed')
  })
})

describe('useM11MapCamera initial view', () => {
  it('fits the China bounds instead of a fixed zoom, so every viewport starts on the same extent', () => {
    const mapRef = { current: null }
    const { result } = renderHook(() => useM11MapCamera({ mapRef }))

    expect(result.current).toEqual({ bounds: CHINA_BOUNDS, fitBoundsOptions: { padding: 32 } })
    expect(result.current).not.toHaveProperty('zoom')
  })

  it('still starts on a known basin fit when one is available at mount', () => {
    const mapRef = { current: null }
    const fitTo = { bounds: [[100, 30], [101, 31]] as [[number, number], [number, number]], padding: 36 }
    const { result } = renderHook(() => useM11MapCamera({ fitTo, mapRef }))

    expect(result.current).toEqual({ bounds: fitTo.bounds, fitBoundsOptions: { padding: 36 } })
  })

  it('never lets the map zoom below the first Tianditu level', () => {
    expect(M11_MAP_MIN_ZOOM).toBe(1)
  })
})
