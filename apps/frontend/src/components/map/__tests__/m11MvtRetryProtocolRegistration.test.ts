import type { AddProtocolAction, GetResourceResponse, RequestParameters } from 'maplibre-gl'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 跨 vi.resetModules() 共享同一个 spy：mock 工厂每次模块重载都会重跑，spy 必须在工厂之外。
const { addProtocolSpy } = vi.hoisted(() => ({ addProtocolSpy: vi.fn() }))

// 只替换 addProtocol；AJAXError 保持真类（处理器非 2xx 时抛它）。
vi.mock('maplibre-gl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('maplibre-gl')>()
  // maplibre-gl 是 UMD 包：命名导出挂在 CJS 互操作对象上，展开不一定可枚举，显式取回真类。
  return { ...actual, AJAXError: actual.AJAXError, addProtocol: addProtocolSpy }
})

const TILE_URL = 'https://example.test/api/v1/tiles/x/1/2/3.pbf'

beforeEach(() => {
  addProtocolSpy.mockClear()
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('m11MapPrimitives registers the nhms-mvt protocol it prefixes tiles with', () => {
  it('registers nhms-mvt on module load and the handler fetches the unprefixed tile URL', async () => {
    const primitives = await import('@/components/map/m11MapPrimitives')
    // 与 primitive 加前缀用的是同一个 helper：前缀与注册的协议名必须一致。
    const { withM11MvtRetryProtocol } = await import('@/components/map/m11MvtRetryProtocol')
    expect(primitives.M11OverlayPrimitive).toBeTypeOf('function')

    const registrations = addProtocolSpy.mock.calls.filter(([name]) => name === 'nhms-mvt')
    expect(registrations).toHaveLength(1)
    const handler = registrations[0][1] as AddProtocolAction

    const payload = new Uint8Array([0x1a, 0x02, 0x08, 0x01])
    const fetchStub = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        new Response(payload.slice().buffer, { status: 200, headers: { 'Cache-Control': 'max-age=60' } }),
    )
    vi.stubGlobal('fetch', fetchStub)

    const prefixed = withM11MvtRetryProtocol(TILE_URL)
    expect(prefixed).toBe(`nhms-mvt://${TILE_URL}`)
    const abortController = new AbortController()
    const result = (await handler(
      { url: prefixed, type: 'arrayBuffer' } as RequestParameters,
      abortController,
    )) as GetResourceResponse<ArrayBuffer>

    expect(fetchStub).toHaveBeenCalledTimes(1)
    expect(fetchStub.mock.calls[0][0]).toBe(TILE_URL)
    expect(fetchStub.mock.calls[0][1]?.signal).toBe(abortController.signal)
    expect(Array.from(new Uint8Array(result.data))).toEqual(Array.from(payload))
    expect(result.cacheControl).toBe('max-age=60')
  })
})
