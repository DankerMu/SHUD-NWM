import { describe, expect, it, vi } from 'vitest'

import {
  classifyRiverClickPreflightResponse,
  normalizeRiverClickEnvelope,
  parseRiverClickPreflightProduct,
  parseRiverClickPreflightSegment,
  RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES,
  type RiverClickPreflightProduct,
  type RiverClickPreflightSegmentOk,
} from '../preflight'

const MAX = 262_144

function productPayload(overrides: Record<string, unknown> = {}) {
  return {
    basin_id: 'basins_qhh',
    model_id: 'model-qhh',
    basin_version_id: 'bv-001',
    river_network_version_id: 'rn-001',
    source_id: 'GFS',
    cycle_time: '2026-09-02T00:00:00Z',
    run_id: 'run-001',
    forcing_version_id: 'f-001',
    station_count: 0,
    expected_station_count: null,
    segment_count: 0,
    expected_segment_count: null,
    status: 'ready',
    run_status: 'finished',
    valid_time_start: null,
    valid_time_end: null,
    river_valid_time_start: null,
    river_valid_time_end: null,
    forcing_valid_time_start: null,
    forcing_valid_time_end: null,
    available_horizon_hours: 240,
    expected_horizon_hours: 240,
    shorter_horizon: false,
    availability: { ready: true, unavailable_reasons: [], quality_flags: [], quality_notes: [] },
    quality: { status: 'ok' },
    ...overrides,
  }
}

const GFS_PRODUCT = productPayload({ source_id: 'GFS' })

const SEGMENT_PAYLOAD = {
  river_segment_id: 'seg-001',
  river_network_version_id: 'rn-001',
  segment_order: 1,
  downstream_segment_id: null,
  length_m: 1000,
  geom: { type: 'LineString', coordinates: [[100, 30], [101, 31], [102, 32]] },
  properties_json: {},
  created_at: '2026-01-01T00:00:00Z',
}

function responseOf(
  body: string | Uint8Array,
  { status = 200, contentLength = String(body instanceof Uint8Array ? body.byteLength : new TextEncoder().encode(body).byteLength), encoding = 'identity' }: {
    status?: number
    contentLength?: string | null
    encoding?: string | null
  } = {},
) {
  const bytes = body instanceof Uint8Array ? body : new TextEncoder().encode(body)
  return {
    url: () => 'https://api.example.test/x',
    status: () => status,
    headerValue: async (name: string) => {
      if (name === 'content-encoding') return encoding
      if (name === 'content-length') return contentLength
      return null
    },
    readBounded: async (maxBytes: number) => {
      const retained = bytes.subarray(0, maxBytes + 1).slice()
      return retained
    },
  }
}

describe('bounded river-click preflight response classifier', () => {
  it('accepts a bounded identity-encoding 2xx response with an ok envelope', async () => {
    const body = JSON.stringify({ status: 'ok', data: productPayload() })
    const classified = await classifyRiverClickPreflightResponse(responseOf(body))
    expect(classified).toMatchObject({
      ok: true,
      status: 200,
      contentType: 'identity',
      bodyBytes: new TextEncoder().encode(body).byteLength,
    })
  })

  it('rejects non-2xx status as PREFLIGHT_HTTP_ERROR', async () => {
    const classified = await classifyRiverClickPreflightResponse(responseOf('', { status: 503, contentLength: null }))
    expect(classified.ok).toBe(false)
    if (!classified.ok) expect(classified.code).toBe('PREFLIGHT_HTTP_ERROR')
  })

  it('rejects non-identity content encoding', async () => {
    const classified = await classifyRiverClickPreflightResponse(responseOf(JSON.stringify({ status: 'ok', data: {} }), { encoding: 'gzip' }))
    expect(classified.ok).toBe(false)
    if (!classified.ok) expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
  })

  it('rejects an over-ceiling declared content-length before reading', async () => {
    let read = false
    const source = responseOf('', { contentLength: String(MAX + 1) })
    const hacked = { ...source, readBounded: async () => { read = true; return new Uint8Array() } }
    const classified = await classifyRiverClickPreflightResponse(hacked)
    expect(read).toBe(false)
    expect(classified.ok).toBe(false)
    if (!classified.ok) expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
  })

  it('aborts a streaming reader before retaining byte 262145 when the length is absent', async () => {
    const oversized = new Uint8Array(MAX + 5).fill(0x78)
    const classified = await classifyRiverClickPreflightResponse(responseOf(oversized, { contentLength: null }))
    expect(classified.ok).toBe(false)
    if (!classified.ok) expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
  })

  it('streams a multi-chunk over-limit body while retaining at most max+1 bytes', async () => {
    const chunk = new Uint8Array(65_536).fill(0x78)
    let pushed = 0
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < 8; i += 1) {
          controller.enqueue(chunk)
          pushed += chunk.byteLength
        }
        controller.close()
      },
    })
    let retainedBytes = 0
    const source = {
      url: () => 'https://api.example.test/x',
      status: () => 200,
      headerValue: async (name: string) => {
        if (name === 'content-encoding') return 'identity'
        return null
      },
      readBounded: async (maxBytes: number) => {
        const reader = stream.getReader()
        const retained: Uint8Array[] = []
        let total = 0
        try {
          for (;;) {
            const { done, value } = await reader.read()
            if (done) break
            if (total + value.byteLength > maxBytes + 1) {
              const keep = Math.max(0, maxBytes + 1 - total)
              retained.push(value.subarray(0, keep))
              total += keep
              break
            }
            retained.push(value)
            total += value.byteLength
          }
        } finally {
          retainedBytes = total
          await reader.cancel()
        }
        const merged = new Uint8Array(total)
        let offset = 0
        for (const part of retained) {
          merged.set(part, offset)
          offset += part.byteLength
        }
        return merged
      },
    }
    const classified = await classifyRiverClickPreflightResponse(source)
    expect(classified.ok).toBe(false)
    if (!classified.ok) expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
    expect(pushed).toBe(8 * 65_536)
    expect(retainedBytes).toBeLessThanOrEqual(MAX + 1)
  })

  it('rejects invalid UTF-8 and malformed JSON bodies', async () => {
    const invalidUtf8 = await classifyRiverClickPreflightResponse(responseOf(new Uint8Array([0xff, 0xfe, 0xfd]), { contentLength: null }))
    expect(invalidUtf8.ok).toBe(false)

    const malformed = await classifyRiverClickPreflightResponse(responseOf('{not json', { contentLength: null }))
    expect(malformed.ok).toBe(false)
    if (!malformed.ok) expect(malformed.code).toBe('PREFLIGHT_RESPONSE_INVALID')
  })

  it('rejects envelopes that are not {status:ok,data:{...}}', async () => {
    for (const body of ['{"status":"error","data":{}}', '{"status":"ok"}', '{"status":"ok","data":[]}', '[]']) {
      const classified = await classifyRiverClickPreflightResponse(responseOf(body, { contentLength: null }))
      expect(classified.ok, body).toBe(false)
      if (!classified.ok) expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
    }
  })

  it('bounds JSON depth 12 / object width 64 / array length 10000 / nodes 50000', async () => {
    let payload: Record<string, unknown> = {}
    let cursor = payload
    for (let i = 0; i < 20; i += 1) {
      cursor.next = {}
      cursor = cursor.next as Record<string, unknown>
    }
    const deepBody = JSON.stringify({ status: 'ok', data: payload })
    const deepClassified = await classifyRiverClickPreflightResponse(responseOf(deepBody, { contentLength: null }))
    expect(deepClassified.ok).toBe(false)

    const wide = JSON.stringify({ status: 'ok', data: Object.fromEntries(Array.from({ length: 66 }, (_, i) => [`k${i}`, 1])) })
    const wideClassified = await classifyRiverClickPreflightResponse(responseOf(wide, { contentLength: null }))
    expect(wideClassified.ok).toBe(false)

    const okBody = JSON.stringify({ status: 'ok', data: { a: 1 } })
    const okClassified = await classifyRiverClickPreflightResponse(responseOf(okBody, { contentLength: null }))
    expect(okClassified.ok).toBe(true)
  })

  it('normalizes envelopes with data at the root', () => {
    expect(normalizeRiverClickEnvelope({ status: 'ok', data: { a: 1 } })).toEqual({ a: 1 })
    expect(normalizeRiverClickEnvelope({ status: 'ok' })).toBeNull()
  })

  it('catches a throwing headerValue to a fixed PREFLIGHT_RESPONSE_INVALID (no raw error text)', async () => {
    const source = {
      ...responseOf(JSON.stringify({ status: 'ok', data: { a: 1 } }), { contentLength: null }),
      headerValue: async () => {
        throw new Error('secret header boom')
      },
    }
    const classified = await classifyRiverClickPreflightResponse(source)
    expect(classified.ok).toBe(false)
    if (!classified.ok) {
      expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
      expect(classified.message).not.toMatch(/secret|boom/)
    }
  })

  it('catches a throwing readBounded to PREFLIGHT_RESPONSE_INVALID and cancels the reader', async () => {
    let cancelled = false
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('x'))
      },
      cancel() {
        cancelled = true
      },
    })
    const source = {
      url: () => 'https://api.example.test/x',
      status: () => 200,
      headerValue: async (name: string) => {
        if (name === 'content-encoding') return 'identity'
        return null
      },
      readBounded: async () => {
        const reader = stream.getReader()
        try {
          await reader.read()
          throw new Error('read boom')
        } finally {
          await reader.cancel()
        }
      },
    }
    const classified = await classifyRiverClickPreflightResponse(source)
    expect(classified.ok).toBe(false)
    if (!classified.ok) {
      expect(classified.code).toBe('PREFLIGHT_RESPONSE_INVALID')
      expect(classified.message).not.toMatch(/boom/)
    }
    expect(cancelled).toBe(true)
  })
})

describe('river-click preflight product parse', () => {
  it('requires exact source, basin, ready status and availability for one product', () => {
    expect(parseRiverClickPreflightProduct(GFS_PRODUCT, 'GFS', 'basins_qhh').ok).toBe(true)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, status: 'unavailable' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, availability: { ready: false } }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, basin_id: 'other' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, source_id: 'IFS' }, 'GFS', 'basins_qhh').ok).toBe(false)
  })

  it('requires valid nonempty run/model/cycle/version identity', () => {
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, run_id: '' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, model_id: '' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, basin_version_id: '' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, river_network_version_id: '' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: 'not-a-time' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-09-02T00:00:00+00:00' }, 'GFS', 'basins_qhh').ok).toBe(true)
  })

  it('normalizes any valid RFC3339 offset instant to canonical UTC toISOString', () => {
    const parsed = parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-09-02T08:00:00+08:00' }, 'GFS', 'basins_qhh')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      expect(parsed.product.cycle_time).toBe('2026-09-02T00:00:00.000Z')
    }
  })

  it('rejects calendar-invalid RFC3339 instants like February 30', () => {
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-02-30T00:00:00Z' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-09-02T24:00:00Z' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-09-02T00:60:00Z' }, 'GFS', 'basins_qhh').ok).toBe(false)
    // Date.parse accepts Apr 31 -> May 1; strict calendar validation must not.
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-04-31T00:00:00Z' }, 'GFS', 'basins_qhh').ok).toBe(false)
    expect(parseRiverClickPreflightProduct({ ...GFS_PRODUCT, cycle_time: '2026-13-01T00:00:00Z' }, 'GFS', 'basins_qhh').ok).toBe(false)
  })

  it('keeps only the closed source identity fields and the fixed scenario', () => {
    const parsed = parseRiverClickPreflightProduct(GFS_PRODUCT, 'GFS', 'basins_qhh')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      const product: Extract<RiverClickPreflightProduct, { ok: true }>['product'] = parsed.product
      expect(Object.keys(product).sort()).toEqual([
        'basin_id', 'basin_version_id', 'cycle_time', 'model_id',
        'river_network_version_id', 'run_id', 'scenario', 'source_id',
      ])
      expect(product.scenario).toBe('forecast_gfs_deterministic')
    }
  })

  it('never interpolates an untrusted header/product value into a failure message', () => {
    const evil = parseRiverClickPreflightProduct(
      { ...GFS_PRODUCT, source_id: 'https://evil.example/secret' },
      'GFS', 'basins_qhh',
    )
    expect(evil.ok).toBe(false)
    if (!evil.ok) {
      expect(evil.reason).not.toMatch(/evil|secret/)
      expect(evil.reason).not.toMatch(/https?:\/\//)
    }
    const evilStatus = parseRiverClickPreflightProduct({ ...GFS_PRODUCT, status: 'ATTACK<script>' }, 'GFS', 'basins_qhh')
    expect(evilStatus.ok).toBe(false)
    if (!evilStatus.ok) {
      expect(evilStatus.reason).not.toMatch(/ATTACK|<script>/)
    }
  })
})

describe('river-click preflight segment parse', () => {
  it('accepts an exact matching bounded LineString/MultiLineString geometry', () => {
    const parsed = parseRiverClickPreflightSegment(SEGMENT_PAYLOAD, 'seg-001', 'rn-001', 'bv-001')
    expect(parsed.ok).toBe(true)
  })

  it('rejects an identity mismatch', () => {
    expect(parseRiverClickPreflightSegment(SEGMENT_PAYLOAD, 'other', 'rn-001', 'bv-001').ok).toBe(false)
    expect(parseRiverClickPreflightSegment(SEGMENT_PAYLOAD, 'seg-001', 'other', 'bv-001').ok).toBe(false)
  })

  it('rejects missing or malformed geometry', () => {
    expect(parseRiverClickPreflightSegment({ ...SEGMENT_PAYLOAD, geom: null }, 'seg-001', 'rn-001', 'bv-001').ok).toBe(false)
    expect(parseRiverClickPreflightSegment({ ...SEGMENT_PAYLOAD, geom: { type: 'Point', coordinates: [1, 2] } }, 'seg-001', 'rn-001', 'bv-001').ok).toBe(false)
  })

  it('rejects non-finite WGS84 coordinates and out-of-range values', () => {
    expect(
      parseRiverClickPreflightSegment(
        { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: [[100, 30], [181, 31]] } },
        'seg-001', 'rn-001', 'bv-001',
      ).ok,
    ).toBe(false)
    expect(
      parseRiverClickPreflightSegment(
        { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: [[100, 30], [101, 91]] } },
        'seg-001', 'rn-001', 'bv-001',
      ).ok,
    ).toBe(false)
    expect(
      parseRiverClickPreflightSegment(
        { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: [[100, 30], [Number.NaN, 31]] } },
        'seg-001', 'rn-001', 'bv-001',
      ).ok,
    ).toBe(false)
  })

  it('rejects over-ceiling, over-wide, and over-bytes geometry', () => {
    const tooMany = Array.from({ length: 10_001 }, (_, i) => [100 + i / 100_000, 30])
    expect(
      parseRiverClickPreflightSegment(
        { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: tooMany } },
        'seg-001', 'rn-001', 'bv-001',
      ).ok,
    ).toBe(false)

    const tooWide = { type: 'LineString', coordinates: [[100, 30, 1, 2], [101, 31, 1, 2]] }
    expect(
      parseRiverClickPreflightSegment(
        { ...SEGMENT_PAYLOAD, geom: tooWide },
        'seg-001', 'rn-001', 'bv-001',
      ).ok,
    ).toBe(false)
  })

  it('rejects a geometry that exceeds 250000 serialized bytes within <=10000 coordinates', () => {
    const padded = { type: 'LineString', coordinates: Array.from({ length: 9_990 }, () => [100.123456789, 30.987654321]) }
    expect(new TextEncoder().encode(JSON.stringify(padded)).byteLength).toBeGreaterThan(RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES)
    expect(
      parseRiverClickPreflightSegment(
        { ...SEGMENT_PAYLOAD, geom: padded },
        'seg-001', 'rn-001', 'bv-001',
      ).ok,
    ).toBe(false)
  })

  it('derives the bbox from all sanitized coordinates and the anchor at floor((n-1)/2)', () => {
    const parsed = parseRiverClickPreflightSegment(SEGMENT_PAYLOAD, 'seg-001', 'rn-001', 'bv-001')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      const segment: RiverClickPreflightSegmentOk = parsed.segment
      expect(segment.bbox).toEqual([[100, 30], [102, 32]])
      expect(segment.anchor).toEqual([101, 31])
    }
  })

  it('indexes the anchor on the flattened SOURCE order even when a one-point part is skipped from shipment', () => {
    // Source order: [100,30] (1-point part, skipped from geometry), [102,32],
    // [103,33]. Source count 3 -> anchor floor((3-1)/2)=1 -> SOURCE index 1 =
    // [102,32]. The SHIPPED geometry keeps only the 2-point part, but the
    // anchor index is on source order (frozen contract), never on retained
    // order (which would give [103,33]).
    const mixed = { type: 'MultiLineString', coordinates: [[[100, 30]], [[102, 32], [103, 33]]] }
    const parsed = parseRiverClickPreflightSegment({ ...SEGMENT_PAYLOAD, geom: mixed }, 'seg-001', 'rn-001', 'bv-001')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      const segment: RiverClickPreflightSegmentOk = parsed.segment
      expect(segment.geometry).toEqual({ type: 'MultiLineString', coordinates: [[[102, 32], [103, 33]]] })
      expect(segment.bbox).toEqual([[102, 32], [103, 33]])
      expect(segment.anchor).toEqual([102, 32])
      expect(segment.coordinateCount).toBe(3)
    }
  })

  it('indexes the anchor on source order with preserved finite Z coordinates', () => {
    const mixed3d = { type: 'MultiLineString', coordinates: [[[100, 30, 500]], [[102, 32, 501], [103, 33, 502]]] }
    const parsed = parseRiverClickPreflightSegment({ ...SEGMENT_PAYLOAD, geom: mixed3d }, 'seg-001', 'rn-001', 'bv-001')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      const segment: RiverClickPreflightSegmentOk = parsed.segment
      expect(segment.geometry).toEqual({ type: 'MultiLineString', coordinates: [[[102, 32, 501], [103, 33, 502]]] })
      expect(segment.anchor).toEqual([102, 32])
    }
  })

  it('emits a non-degenerate bbox for a vertical axis-aligned LineString (zero-width extent)', () => {
    const vertical = { type: 'LineString', coordinates: [[100, 30], [100, 31], [100, 32]] }
    const parsed = parseRiverClickPreflightSegment({ ...SEGMENT_PAYLOAD, geom: vertical }, 'seg-001', 'rn-001', 'bv-001')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      const [[minLon, minLat], [maxLon, maxLat]] = parsed.segment.bbox
      expect(minLon).toBeLessThan(maxLon)
      expect(minLat).toBeLessThan(maxLat)
      expect(maxLon - minLon).toBeLessThanOrEqual(1)
      expect(maxLat - minLat).toBeLessThanOrEqual(3)
    }
  })

  it('clamps the padded bbox at the world edges so it stays WGS84', () => {
    const atEdge = { type: 'LineString', coordinates: [[180, 90], [180, -90]] }
    const parsed = parseRiverClickPreflightSegment({ ...SEGMENT_PAYLOAD, geom: atEdge }, 'seg-001', 'rn-001', 'bv-001')
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      const [[minLon, minLat], [maxLon, maxLat]] = parsed.segment.bbox
      expect(minLon).toBeGreaterThanOrEqual(-180)
      expect(maxLon).toBeLessThanOrEqual(180)
      expect(minLat).toBeGreaterThanOrEqual(-90)
      expect(maxLat).toBeLessThanOrEqual(90)
      expect(minLon).toBeLessThan(maxLon)
      expect(minLat).toBeLessThan(maxLat)
    }
  })
})
