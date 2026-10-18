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

  it('reports a non-2xx response as an error with the pre-change absent shape', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 500 })))
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const { result } = renderHook(() => useNationalBasinGeo(true))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.domain).toBeNull()
    expect(result.current.river).toBeNull()
    expect(result.current.error).toBe('national basin domain HTTP 500')
  })

  it('reports a FeatureCollection whose features is not an array as malformed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ type: 'FeatureCollection', features: null }), { status: 200 })),
    )
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const { result } = renderHook(() => useNationalBasinGeo(true))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.domain).toBeNull()
    expect(result.current.river).toBeNull()
    expect(result.current.error).toBe('national basin domain payload malformed')
  })

  it('reports a non-JSON body as malformed', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('not json', { status: 200 })))
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const { result } = renderHook(() => useNationalBasinGeo(true))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.domain).toBeNull()
    expect(result.current.river).toBeNull()
    expect(result.current.error).toBe('national basin domain payload malformed')
  })

  it('does not cache a failure: re-activation in the same module instance retries and clears the error', async () => {
    const fetchMock = vi
      .fn(async (): Promise<Response> => new Response(null, { status: 500 }))
      .mockResolvedValueOnce(new Response(null, { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ type: 'FeatureCollection', features: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const { result, rerender } = renderHook(({ active }) => useNationalBasinGeo(active), {
      initialProps: { active: true },
    })
    await waitFor(() => expect(result.current.error).toBe('national basin domain HTTP 500'))
    expect(result.current.loading).toBe(false)

    rerender({ active: false })
    rerender({ active: true })
    await waitFor(() => expect(result.current.domain).not.toBeNull())

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(result.current.domain).toEqual({ type: 'FeatureCollection', features: [] })
    expect(result.current.error).toBeNull()
    expect(result.current.loading).toBe(false)
  })

  it('reports a network rejection through its Error message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (): Promise<Response> => {
        throw new TypeError('Failed to fetch')
      }),
    )
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const { result } = renderHook(() => useNationalBasinGeo(true))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.domain).toBeNull()
    expect(result.current.river).toBeNull()
    expect(result.current.error).toBe('Failed to fetch')
  })

  it('shares one inflight failure across concurrent consumers without caching it', async () => {
    let resolveFirst: (response: Response) => void = () => {}
    const fetchMock = vi
      .fn(async (): Promise<Response> => new Response(null, { status: 500 }))
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveFirst = resolve
          }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { useNationalBasinGeo } = await import('@/pages/m11/useNationalBasinGeo')

    const first = renderHook(() => useNationalBasinGeo(true))
    const second = renderHook(() => useNationalBasinGeo(true))
    expect(fetchMock).toHaveBeenCalledTimes(1)

    resolveFirst(new Response(null, { status: 503 }))
    await waitFor(() => expect(first.result.current.error).toBe('national basin domain HTTP 503'))
    await waitFor(() => expect(second.result.current.error).toBe('national basin domain HTTP 503'))

    const third = renderHook(() => useNationalBasinGeo(true))
    await waitFor(() => expect(third.result.current.error).toBe('national basin domain HTTP 500'))
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
