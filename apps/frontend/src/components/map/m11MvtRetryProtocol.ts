import { AJAXError, addProtocol } from 'maplibre-gl'
import type { GetResourceResponse, RequestParameters } from 'maplibre-gl'

/**
 * M11 MVT 瓦片的冷生成繁忙重试（#2537，fixture D1）。
 *
 * 后端冷生成 gate 满载时回 `503` + `Retry-After: 1` + `{"error":{"code":"MVT_COLD_GENERATION_BUSY"}}`（#2346）。
 * MapLibre 的内置 fetch 对 `http(s)` URL 不可拦截，只有 `<scheme>://` 形式的 URL 才会查自定义协议表，
 * 所以 M11 surface 的 MVT vector source 在 `<Source tiles>` 处加 `nhms-mvt://` 前缀，本处理器剥掉前缀后
 * 用原 URL 取瓦片。worker 里的瓦片请求经 `GR` 消息转到主线程执行本处理器，解析仍在 worker。
 */
export const M11_MVT_RETRY_PROTOCOL = 'nhms-mvt'

const PREFIX = `${M11_MVT_RETRY_PROTOCOL}://`
const BUSY_CODE = 'MVT_COLD_GENERATION_BUSY'
const MAX_RETRIES = 3
const DEFAULT_RETRY_AFTER_MS = 1000
const MIN_RETRY_AFTER_MS = 250
const MAX_RETRY_AFTER_MS = 5000
const MAX_JITTER_MS = 250
// maplibre 的 isAbortError 只比对 message，必须是这个字面量。
const ABORT_ERROR = 'AbortError'

export interface M11MvtRetryDeps {
  fetch: (url: string, init: RequestInit) => Promise<Response>
  /** 等待 ms 毫秒；signal abort 时必须立即清掉定时器并 reject。 */
  sleep: (ms: number, signal: AbortSignal) => Promise<void>
  /** [0, 1) 随机数，用于抖动。 */
  random: () => number
}

/** 加 `nhms-mvt://` 前缀；已有前缀时原样返回（幂等）。 */
export function withM11MvtRetryProtocol(url: string): string {
  return url.startsWith(PREFIX) ? url : `${PREFIX}${url}`
}

function stripPrefix(url: string): string {
  return url.startsWith(PREFIX) ? url.slice(PREFIX.length) : url
}

function abortError(): Error {
  return new Error(ABORT_ERROR)
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError())
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError())
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

const defaultDeps: M11MvtRetryDeps = {
  fetch: (url, init) => fetch(url, init),
  sleep: defaultSleep,
  random: Math.random,
}

/** `Retry-After` 只认十进制秒数；HTTP-date、负数、空串一律按默认 1 s。结果夹在 [0.25 s, 5 s]。 */
function retryAfterMs(value: string | null): number {
  const trimmed = value?.trim() ?? ''
  const ms = /^\d+(\.\d+)?$/.test(trimmed) ? Number(trimmed) * 1000 : DEFAULT_RETRY_AFTER_MS
  return Math.min(MAX_RETRY_AFTER_MS, Math.max(MIN_RETRY_AFTER_MS, ms))
}

function hasBusyCode(body: ArrayBuffer): boolean {
  try {
    const parsed = JSON.parse(new TextDecoder().decode(body)) as { error?: { code?: unknown } } | null
    return parsed?.error?.code === BUSY_CODE
  } catch {
    return false
  }
}

/**
 * `addProtocol` 处理器本体。只有 `503` 且（带 `Retry-After` 头，或响应体 `error.code` 为繁忙码）才重试，
 * 最多重试 3 次（至多 4 次请求）；其余状态与重试用完时抛 maplibre 导出的 `AJAXError`——它注册了跨线程
 * 序列化，`status` 能回到 worker，404 仍按空瓦片处理。abort 时抛 message 为 `AbortError` 的 Error，
 * 且不再发下一次请求。
 */
export async function m11MvtRetryLoad(
  params: RequestParameters,
  abortController: AbortController,
  deps: Partial<M11MvtRetryDeps> = {},
): Promise<GetResourceResponse<ArrayBuffer>> {
  const { fetch: doFetch, sleep, random } = { ...defaultDeps, ...deps }
  const { signal } = abortController
  const url = stripPrefix(params.url)

  for (let attempt = 0; ; attempt += 1) {
    if (signal.aborted) throw abortError()
    let response: Response
    let body: ArrayBuffer
    try {
      response = await doFetch(url, { signal })
      body = await response.arrayBuffer()
    } catch (error) {
      if (signal.aborted) throw abortError()
      throw error
    }
    if (signal.aborted) throw abortError()

    if (response.ok) {
      return {
        data: body,
        cacheControl: response.headers.get('Cache-Control'),
        expires: response.headers.get('Expires'),
      }
    }

    const retryAfter = response.headers.get('Retry-After')
    const retryable = response.status === 503 && (retryAfter !== null || hasBusyCode(body))
    if (!retryable || attempt >= MAX_RETRIES) {
      const contentType = response.headers.get('Content-Type')
      const blob = new Blob([body], contentType ? { type: contentType } : undefined)
      throw new AJAXError(response.status, response.statusText, url, blob)
    }

    try {
      await sleep(retryAfterMs(retryAfter) + random() * MAX_JITTER_MS, signal)
    } catch (error) {
      if (signal.aborted) throw abortError()
      throw error
    }
    // 循环顶部再查一次 signal：即便注入的 sleep 没理会 abort，也绝不发下一次请求。
  }
}

let registered = false

/** 向 MapLibre 注册 `nhms-mvt` 协议；重复调用无副作用。 */
export function registerM11MvtRetryProtocol(): void {
  if (registered) return
  addProtocol(M11_MVT_RETRY_PROTOCOL, (params, abortController) => m11MvtRetryLoad(params, abortController))
  registered = true
}
