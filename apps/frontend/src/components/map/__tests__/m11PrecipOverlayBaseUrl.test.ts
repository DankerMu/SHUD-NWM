import { describe, expect, it, vi } from 'vitest'

import { resolveM11PrecipOverlay } from '@/components/map/m11PrecipOverlay'
import { m11SourceCycleKey, type PrecipIndex, type PrecipIndexState } from '@/stores/overviewData'
import { precipIndex } from '@/test/overviewDataFixture'

/**
 * 跨源部署（`VITE_API_BASE_URL`）的 oracle，单独成文件：模块级 mock 一旦装上就作用于整个文件，
 * 而 `m11PrecipOverlay.test.ts` 的既有断言全是空 base 下的字面 URL。这里只改 `buildApiUrl` 的
 * base 入参、仍走**真实**实现，故断的是「调用点确实经过 buildApiUrl」+「URL 形状不变」两件事，
 * 而不是一个手搓假实现自己的输出。
 */
const TEST_API_BASE = 'https://api.example.com'

vi.mock('@/api/base', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/base')>()
  return { ...actual, buildApiUrl: (path: string) => actual.buildApiUrl(path, TEST_API_BASE) }
})

const CYCLE = '2026-05-18T00:00:00Z'
const VALID_TIME = '2026-05-18T03:00:00Z'

function availableIndex(): PrecipIndexState {
  return { status: 'available', index: { ...(precipIndex as PrecipIndex), valid_times: [VALID_TIME] } }
}

describe('resolveM11PrecipOverlay under a cross-origin VITE_API_BASE_URL', () => {
  it('honours the configured api base url instead of emitting a page-origin relative path', () => {
    const model = resolveM11PrecipOverlay({
      precip: true,
      concreteSource: 'gfs',
      cycle: CYCLE,
      validTime: VALID_TIME,
      precipIndexByCycle: { [m11SourceCycleKey('gfs', CYCLE)]: availableIndex() },
      enrichmentSkipped: false,
    })

    // index 走 openapi client（已带 `baseUrl: apiBaseUrl`）；PNG 留裸相对路径就会解析到页面源
    // → 404 → 透明栅格而模型仍报 visible（`frontend-production-readiness` spec 第 27 行）。
    expect(model.url).toBe(`${TEST_API_BASE}/api/v1/precip/gfs/${CYCLE}/${VALID_TIME}.png`)
    expect(model.hiddenReason).toBeNull()
  })
})
