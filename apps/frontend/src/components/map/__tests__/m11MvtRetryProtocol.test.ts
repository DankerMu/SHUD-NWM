import { AJAXError, addProtocol } from 'maplibre-gl'
import type { RequestParameters } from 'maplibre-gl'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  M11_MVT_RETRY_PROTOCOL,
  m11MvtRetryLoad,
  registerM11MvtRetryProtocol,
  withM11MvtRetryProtocol,
  type M11MvtRetryDeps,
} from '@/components/map/m11MvtRetryProtocol'

// 只替换 addProtocol（注册幂等断言要数调用次数）；AJAXError 必须仍是真类——
// 下面按 `instanceof AJAXError` + `status` 断言失败瓦片与 MapLibre 默认 fetch 同形。
vi.mock('maplibre-gl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('maplibre-gl')>()
  // maplibre-gl 是 UMD 包：命名导出挂在 CJS 互操作对象上，展开不一定可枚举，显式取回真类。
  return { ...actual, AJAXError: actual.AJAXError, addProtocol: vi.fn() }
})

const TILE_URL = 'https://nwm.example/api/v1/tiles/hydro-national/gfs/c/q_down/v/7/111/43.pbf?_mvt_cache_version=aa25'
const BUSY_BODY = JSON.stringify({ error: { code: 'MVT_COLD_GENERATION_BUSY', message: 'busy' } })

function bytes(text: string): ArrayBuffer {
  return new TextEncoder().encode(text).buffer as ArrayBuffer
}

/** 最小 Response 形状：处理器只读 ok/status/statusText/headers/arrayBuffer。 */
function response(status: number, options: { headers?: Record<string, string>; body?: string; statusText?: string } = {}) {
  const headers = new Headers(options.headers ?? {})
  const payload = bytes(options.body ?? '')
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: options.statusText ?? `status ${status}`,
    headers,
    arrayBuffer: vi.fn(async () => payload),
  } as unknown as Response
}

function busy503(retryAfter: string | null = '1', body: string = BUSY_BODY) {
  return response(503, {
    statusText: 'Service Unavailable',
    headers: retryAfter === null ? { 'Content-Type': 'application/json' } : { 'Retry-After': retryAfter, 'Content-Type': 'application/json' },
    body,
  })
}

function tileOk(text: string, headers: Record<string, string> = {}) {
  return response(200, { body: text, headers })
}

function params(url = withM11MvtRetryProtocol(TILE_URL)): RequestParameters {
  return { url, type: 'arrayBuffer' } as RequestParameters
}

function harness(responses: Response[], random = 0) {
  const queue = [...responses]
  const fetch = vi.fn(async (_url: string, _init?: RequestInit) => {
    const next = queue.shift()
    if (!next) throw new Error('unexpected extra request')
    return next
  })
  const sleep = vi.fn(async (_ms: number, _signal: AbortSignal) => undefined)
  const deps: M11MvtRetryDeps = { fetch, sleep, random: () => random }
  return { fetch, sleep, deps }
}

function decode(buffer: ArrayBuffer) {
  return new TextDecoder().decode(buffer)
}

describe('withM11MvtRetryProtocol', () => {
  it('prefixes the scheme exactly once and the loader strips it back to the original URL', async () => {
    expect(M11_MVT_RETRY_PROTOCOL).toBe('nhms-mvt')
    const wrapped = withM11MvtRetryProtocol(TILE_URL)
    expect(wrapped).toBe(`nhms-mvt://${TILE_URL}`)
    expect(withM11MvtRetryProtocol(wrapped)).toBe(wrapped)
    // MapLibre 的 getProtocol 取 `://` 之前的部分查表：必须恰好是注册名。
    expect(wrapped.substring(0, wrapped.indexOf('://'))).toBe(M11_MVT_RETRY_PROTOCOL)
    // {z}/{x}/{y} 模板占位符原样保留，由 MapLibre 在包装后的 URL 上替换。
    expect(withM11MvtRetryProtocol('https://h/{z}/{x}/{y}.pbf')).toBe('nhms-mvt://https://h/{z}/{x}/{y}.pbf')

    const { fetch, deps } = harness([tileOk('pbf')])
    await m11MvtRetryLoad(params(wrapped), new AbortController(), deps)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe(TILE_URL)
  })
})

describe('m11MvtRetryLoad', () => {
  it('returns the tile bytes and expiry headers like the built-in fetch path', async () => {
    const { fetch, sleep, deps } = harness([tileOk('tile-bytes', { 'Cache-Control': 'max-age=60', Expires: 'Wed, 01 Oct 2026 00:00:00 GMT' })])
    const controller = new AbortController()
    const result = await m11MvtRetryLoad(params(), controller, deps)
    expect(decode(result.data)).toBe('tile-bytes')
    expect(result.cacheControl).toBe('max-age=60')
    expect(result.expires).toBe('Wed, 01 Oct 2026 00:00:00 GMT')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][1]?.signal).toBe(controller.signal)
    expect(sleep).not.toHaveBeenCalled()
  })

  it('retries a busy 503 after Retry-After seconds (+ jitter) and resolves with the second response', async () => {
    const { fetch, sleep, deps } = harness([busy503('1'), tileOk('second')], 0.5)
    const result = await m11MvtRetryLoad(params(), new AbortController(), deps)
    expect(decode(result.data)).toBe('second')
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls.map((call) => call[0])).toEqual([TILE_URL, TILE_URL])
    // 1 s + 0.5 * 250 ms 抖动。
    expect(sleep).toHaveBeenCalledTimes(1)
    expect(sleep.mock.calls[0][0]).toBe(1125)
  })

  it('also retries a 503 carrying only the busy body code (Retry-After not CORS-exposed)', async () => {
    const { fetch, sleep, deps } = harness([busy503(null), tileOk('after-busy')])
    const result = await m11MvtRetryLoad(params(), new AbortController(), deps)
    expect(decode(result.data)).toBe('after-busy')
    expect(fetch).toHaveBeenCalledTimes(2)
    // 无头时按默认 1 s，random=0 ⇒ 无抖动。
    expect(sleep.mock.calls[0][0]).toBe(1000)
  })

  it('stops after 3 retries (4 requests) and fails with an AJAXError keeping status 503', async () => {
    const { fetch, sleep, deps } = harness([busy503(), busy503(), busy503(), busy503(), tileOk('never')])
    const error = await m11MvtRetryLoad(params(), new AbortController(), deps).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(AJAXError)
    expect((error as AJAXError).status).toBe(503)
    expect((error as AJAXError).statusText).toBe('Service Unavailable')
    // url 是原始 https URL，不是带前缀的 nhms-mvt:// 形式。
    expect((error as AJAXError).url).toBe(TILE_URL)
    expect(fetch).toHaveBeenCalledTimes(4)
    expect(sleep).toHaveBeenCalledTimes(3)
  })

  it.each([
    ['404 empty tile', response(404, { statusText: 'Not Found' }), 404],
    ['500 server error', response(500, { statusText: 'Internal Server Error', body: BUSY_BODY }), 500],
    [
      '503 without Retry-After and without the busy code',
      response(503, { statusText: 'Service Unavailable', body: JSON.stringify({ error: { code: 'OTHER' } }) }),
      503,
    ],
    ['503 with a non-JSON body and no Retry-After', response(503, { statusText: 'Service Unavailable', body: '<html>' }), 503],
  ])('does not retry a %s and preserves the status on the AJAXError', async (_label, first, status) => {
    const { fetch, sleep, deps } = harness([first, tileOk('never')])
    const error = await m11MvtRetryLoad(params(), new AbortController(), deps).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(AJAXError)
    expect((error as AJAXError).status).toBe(status)
    expect((error as AJAXError).url).toBe(TILE_URL)
    expect((error as AJAXError).body).toBeInstanceOf(Blob)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(sleep).not.toHaveBeenCalled()
  })

  it.each([
    ['0', 250],
    ['0.1', 250],
    ['2.5', 2500],
    ['5', 5000],
    ['30', 5000],
    ['Wed, 21 Oct 2026 07:28:00 GMT', 1000],
    ['-3', 1000],
    ['', 1000],
  ])('clamps Retry-After %j to %i ms before jitter', async (retryAfter, expectedMs) => {
    const { sleep, deps } = harness([busy503(retryAfter), tileOk('ok')])
    await m11MvtRetryLoad(params(), new AbortController(), deps)
    expect(sleep.mock.calls[0][0]).toBe(expectedMs)
  })

  it('keeps jitter inside [0, 250) ms', async () => {
    const { sleep, deps } = harness([busy503('1'), tileOk('ok')], 0.999)
    await m11MvtRetryLoad(params(), new AbortController(), deps)
    const waited = sleep.mock.calls[0][0]
    expect(waited).toBeGreaterThanOrEqual(1000)
    expect(waited).toBeLessThan(1250)
  })

  it('rejects with AbortError and sends no further request when aborted during the retry wait', async () => {
    const controller = new AbortController()
    const fetch = vi.fn(async () => busy503())
    // 注入的 sleep 模拟「等待期间 MapLibre 放弃瓦片」：挂起直到 signal abort。
    const sleep = vi.fn(
      (_ms: number, signal: AbortSignal) =>
        new Promise<void>((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(new Error('AbortError')))
          queueMicrotask(() => controller.abort())
        }),
    )
    const error = await m11MvtRetryLoad(params(), controller, { fetch, sleep, random: () => 0 }).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toBe('AbortError')
    expect(error).not.toBeInstanceOf(AJAXError)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('does not issue the next request even if an injected sleep ignores the abort signal', async () => {
    const controller = new AbortController()
    const fetch = vi.fn(async () => busy503())
    const sleep = vi.fn(async () => {
      controller.abort()
    })
    const error = await m11MvtRetryLoad(params(), controller, { fetch, sleep, random: () => 0 }).catch((caught: unknown) => caught)
    expect((error as Error).message).toBe('AbortError')
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('never fetches when the tile is already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    const { fetch, deps } = harness([tileOk('never')])
    const error = await m11MvtRetryLoad(params(), controller, deps).catch((caught: unknown) => caught)
    expect((error as Error).message).toBe('AbortError')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('maps a fetch rejection caused by abort to the message-based AbortError MapLibre recognises', async () => {
    const controller = new AbortController()
    const fetch = vi.fn(async () => {
      controller.abort()
      throw new DOMException('The operation was aborted.', 'AbortError')
    })
    const error = await m11MvtRetryLoad(params(), controller, { fetch, sleep: vi.fn(), random: () => 0 }).catch(
      (caught: unknown) => caught,
    )
    expect((error as Error).message).toBe('AbortError')
  })
})

describe('m11MvtRetryLoad default wait', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('clears the pending retry timer on abort instead of letting it fire', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const fetch = vi.fn(async () => busy503('2'))
    const pending = m11MvtRetryLoad(params(), controller, { fetch, random: () => 0 }).catch((caught: unknown) => caught)
    // 让第一次请求与 body 读取完成，进入等待。
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(1)
    controller.abort()
    const error = await pending
    expect((error as Error).message).toBe('AbortError')
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('waits the Retry-After delay before the next request', async () => {
    vi.useFakeTimers()
    const fetch = vi
      .fn<(url: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(busy503('2'))
      .mockResolvedValueOnce(tileOk('late'))
    const pending = m11MvtRetryLoad(params(), new AbortController(), { fetch, random: () => 0 })
    await vi.advanceTimersByTimeAsync(1_999)
    expect(fetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    const result = await pending
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(decode(result.data)).toBe('late')
  })
})

describe('registerM11MvtRetryProtocol', () => {
  it('registers the nhms-mvt protocol with MapLibre exactly once', () => {
    registerM11MvtRetryProtocol()
    registerM11MvtRetryProtocol()
    expect(vi.mocked(addProtocol)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(addProtocol).mock.calls[0][0]).toBe('nhms-mvt')
    expect(typeof vi.mocked(addProtocol).mock.calls[0][1]).toBe('function')
  })
})
