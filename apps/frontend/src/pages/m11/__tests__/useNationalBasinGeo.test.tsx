import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('useNationalBasinGeo', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('loads only the lightweight domain and never fetches the retired national river GeoJSON', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe('/geo/national-basin-domain.geojson')
      return new Response(JSON.stringify({ type: 'FeatureCollection', features: [] }), {
        status: 200,
        headers: { 'content-type': 'application/geo+json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const { result } = renderHook(() => useNationalBasinGeo(true))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(result.current.domain).toEqual({ type: 'FeatureCollection', features: [] })
    expect(result.current.river).toBeNull()
  })
})
