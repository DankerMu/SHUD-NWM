import { describe, expect, it } from 'vitest'

import { createSourceScenarioSelection } from '@/lib/m11/overviewDataContracts'
import {
  defaultM11QueryState,
  m11QueryHref,
  needsM11QueryReplacement,
  normalizeM11Identifier,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import type { M11Layer } from '@/lib/m11/queryState'

describe('M11 query state helpers', () => {
  it('round-trips supported values', () => {
    const state = parseM11QueryState(
      'source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&layer=discharge&basemap=satellite&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basins_qhh&segmentId=seg-009&q=%E5%B9%B2%E6%B5%81',
    )

    expect(state).toEqual({
      source: 'ifs',
      cycle: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
      layer: 'discharge',
      metStations: false,
      precip: true,
      basemap: 'satellite',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-v1',
      basinId: 'basins_qhh',
      segmentId: 'seg-009',
      q: '干流',
    })

    expect(parseM11QueryState(serializeM11QueryState(state))).toEqual(state)
  })

  it('canonicalizes source values case-insensitively for legacy deep links', () => {
    const state = parseM11QueryState('source=IFS&metStations=1')

    expect(state.source).toBe('ifs')
    expect(serializeM11QueryState(state)).toBe('source=ifs&metStations=1')
    expect(needsM11QueryReplacement('?source=IFS&metStations=1')).toBe(true)
  })

  it('normalizes invalid values to documented defaults', () => {
    const state = parseM11QueryState('source=unknown&basemap=bad&cycle=not-a-date&metStations=0&q=')

    expect(state).toEqual(defaultM11QueryState)
    expect(serializeM11QueryState(state)).toBe('')
    expect(needsM11QueryReplacement('?source=unknown&basemap=bad')).toBe(true)
    expect(needsM11QueryReplacement('')).toBe(false)
  })

  it('rejects reserved-character basinId and keeps a valid basinId single-valued (#338 boundary)', () => {
    // basinId 进 query 后由 normalizeM11Identifier 白名单把关：含 / ? # % 的 id 被拒、不写入 URL，
    // 杜绝伪造/越权流域上下文；合法 basinId 经 parse→serialize 单值往返。
    const rejected = parseM11QueryState('basinId=basin%2Fdemo%3Fbranch%23run%2525')
    expect(rejected.basinId).toBeNull()
    expect(serializeM11QueryState(rejected)).not.toContain('basinId=')

    const accepted = parseM11QueryState('basinId=basins_qhh&basinId=basins_heihe')
    expect(accepted.basinId).toBe('basins_qhh')
    const serialized = serializeM11QueryState(accepted)
    expect(new URLSearchParams(serialized).getAll('basinId')).toEqual(['basins_qhh'])
  })

  it('omits empty or unsupported values on serialization', () => {
    // `source` 用默认值（现为 gfs，#2012 前是 best）：本用例只钉「空/非法值不进 URL」。
    const state = {
      ...defaultM11QueryState,
      source: defaultM11QueryState.source,
      layer: 'discharge' as const,
      basemap: 'vector' as const,
      cycle: 'bad',
      riverNetworkVersionId: 'bad/id',
      segmentId: 'bad/id',
    }

    expect(serializeM11QueryState(state)).toBe('')
  })

  it('flips which source value the canonical URL carries now that the default is gfs', () => {
    // spec frontend-mvt-layer-consumption「Query state carries source, cycle, and precipitation
    // toggle」：`defaultM11QueryState.source` 变 `'gfs'` 后序列化三翻。
    expect(defaultM11QueryState.source).toBe('gfs')
    expect(serializeM11QueryState({ ...defaultM11QueryState, source: 'gfs' })).toBe('')
    expect(serializeM11QueryState({ ...defaultM11QueryState, source: 'best' })).toBe('source=best')
    expect(needsM11QueryReplacement('source=gfs')).toBe(true)
    expect(needsM11QueryReplacement('source=best')).toBe(false)
    // `best` 仍是合法可解析值（流域详情 Best Available 要用），parser 绝不把它改写成 gfs。
    expect(parseM11QueryState('source=best').source).toBe('best')
  })

  it('exports the same short identifier allowlist for path and query segment IDs', () => {
    expect(normalizeM11Identifier('seg-009')).toBe('seg-009')
    expect(normalizeM11Identifier('bad/id')).toBeNull()
    expect(normalizeM11Identifier('x'.repeat(97))).toBeNull()
  })

  it.each([
    ['invalid calendar date', '2026-02-30T00:00:00Z'],
    ['invalid hour', '2026-05-18T24:00:00Z'],
    ['invalid minute', '2026-05-18T00:60:00Z'],
    ['invalid second', '2026-05-18T00:00:60Z'],
    ['timezone-less timestamp', '2026-05-18T00:00:00'],
    ['timezone-less fractional timestamp', '2026-05-18T00:00:00.123456'],
    ['date-only value', '2026-05-18'],
    ['numeric value', '1779062400000'],
    ['bad offset hour', '2026-05-18T00:00:00+24:00'],
    ['bad offset minute', '2026-05-18T00:00:00+08:60'],
    ['unknown local offset', '2026-05-18T00:00:00-00:00'],
    ['overflow after offset', '2026-12-31T24:00:00+08:00'],
  ])('rejects %s for forecast instants', (_label, value) => {
    expect(parseM11QueryState(`cycle=${encodeURIComponent(value)}&validTime=${encodeURIComponent(value)}`)).toMatchObject({
      cycle: null,
      validTime: null,
    })
  })

  it('normalizes valid explicit-offset RFC3339 instants to UTC', () => {
    const state = parseM11QueryState('cycle=2026-05-18T08:30:15.250001%2B08:00&validTime=2026-05-17T23:45:00-02:30')

    expect(state.cycle).toBe('2026-05-18T00:30:15.250Z')
    expect(state.validTime).toBe('2026-05-18T02:15:00.000Z')
  })

  it('accepts RFC3339 fractional seconds beyond milliseconds and documents UTC millisecond precision', () => {
    const state = parseM11QueryState(
      'cycle=2026-05-18T00%3A00%3A00.123456Z&validTime=2026-05-18T00%3A00%3A00.123456Z',
    )

    expect(state.cycle).toBe('2026-05-18T00:00:00.123Z')
    expect(state.validTime).toBe('2026-05-18T00:00:00.123Z')
    expect(serializeM11QueryState(state)).toBe(
      'cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T00%3A00%3A00.123Z',
    )
  })

  it('does not request replacement after parse/serialize canonicalization', () => {
    const canonical = serializeM11QueryState(
      parseM11QueryState(
        'cycle=2026-05-18T08%3A30%3A15.250001%2B08%3A00&validTime=2026-05-18T00%3A00%3A00.123456Z',
      ),
    )

    expect(canonical).toBe('cycle=2026-05-18T00%3A30%3A15.250Z&validTime=2026-05-18T00%3A00%3A00.123Z')
    expect(needsM11QueryReplacement(canonical)).toBe(false)
    expect(needsM11QueryReplacement(`?${canonical}`)).toBe(false)
  })

  it('falls back to the default discharge layer when a stale URL requests the retired hydro variant (#581)', () => {
    // 分享链 / 书签 / 旧路由状态可能仍带 layer=<退役标识>。parser 必须把它当作未知值，
    // 回落到 defaultM11QueryState.layer（=discharge），并且 serialize 后不再写出该值。
    // split-string sentinel：防 naive find-replace 把退役标识符再次悄悄回填到源码。
    const retiredLayerId = 'wat' + 'er-level'
    const state = parseM11QueryState(`layer=${retiredLayerId}`)

    expect(state.layer).toBe('discharge')
    expect(state.layer).toBe(defaultM11QueryState.layer)
    expect(serializeM11QueryState(state)).toBe('')
    expect(serializeM11QueryState(state)).not.toContain(retiredLayerId)
    // 需要 URL 替换：URL 上仍带退役 layer，但 serialize 出的 canonical 串里不再含该值。
    expect(needsM11QueryReplacement(`?layer=${retiredLayerId}`)).toBe(true)
  })

  it('normalizes the legacy met-stations layer into a station overlay query state', () => {
    const state = parseM11QueryState('layer=met-stations')

    expect(state.layer).toBe('discharge')
    expect(state.metStations).toBe(true)
    expect(serializeM11QueryState(state)).toBe('metStations=1')
    expect(serializeM11QueryState(state)).not.toContain('layer=met-stations')
    expect(needsM11QueryReplacement('?layer=met-stations')).toBe(true)
  })

  it('round-trips precip=0 without rewriting the URL away on load', () => {
    // spec frontend-mvt-layer-consumption「Round trip with precipitation disabled」
    const serialized = serializeM11QueryState(parseM11QueryState('precip=0'))

    expect(serialized).toContain('precip=0')
    expect(parseM11QueryState(serialized).precip).toBe(false)
    expect(needsM11QueryReplacement('precip=0')).toBe(false)
    expect(needsM11QueryReplacement('?precip=0')).toBe(false)
  })

  it('serializes an explicitly disabled precipitation state', () => {
    // spec「Serializing an explicit false state」：`queryParamsFromState` 今天丢掉所有 false 布尔，
    // 内部那趟 parse 归一会把 false 吃回默认 true —— precip 必须能穿过这趟归一。
    expect(serializeM11QueryState({ ...defaultM11QueryState, precip: false })).toContain('precip=0')
  })

  it('defaults precipitation on and omits it from the canonical URL', () => {
    // spec「Default precipitation is on」
    const state = parseM11QueryState('')

    expect(state.precip).toBe(true)
    expect(serializeM11QueryState(state)).not.toContain('precip')
    expect(parseM11QueryState('precip=1').precip).toBe(true)
    expect(parseM11QueryState('precip=whatever').precip).toBe(true)
  })

  it('leaves every other boolean serialization untouched', () => {
    // spec「Other booleans keep their existing serialization」：metStations 的 false 仍被省略。
    const serialized = serializeM11QueryState({ ...defaultM11QueryState, metStations: false, precip: false })

    expect(serialized).not.toContain('metStations')
    expect(serialized).toBe('precip=0')
    expect(serializeM11QueryState({ ...defaultM11QueryState, metStations: true })).toBe('metStations=1')
  })

  it('keeps the layer enum discharge-only when a URL asks for precip', () => {
    // spec「Layer enum unchanged」：`layer=precip` 回落 discharge，且类型层拒绝 'precip'。
    expect(parseM11QueryState('layer=precip').layer).toBe('discharge')
    expect(serializeM11QueryState(parseM11QueryState('layer=precip'))).toBe('')
    // @ts-expect-error 'precip' 不是 M11Layer；本行由 `pnpm exec tsc -p tsconfig.app.json` 覆盖（include: src）。
    const rejected: M11Layer = 'precip'
    expect(rejected).toBe('precip')
  })

  it('resolves a restored source=best to gfs at national scale only', () => {
    // spec map-layer-timeline-controls「a restored URL with `source=best` at national scale MUST
    // resolve to `gfs`」——归一在 selection 层，不在 parser（否则 serialize 往返会改写用户 URL）。
    const parsed = parseM11QueryState('source=best')
    expect(parsed.source).toBe('best')

    const national = createSourceScenarioSelection(parsed, ['GFS', 'IFS'], { scale: 'national' })
    expect(national.requestedSource).toBe('gfs')
    expect(national.resolvedSource).toBe('GFS')
    expect(national.provenanceLabel).not.toContain('Best Available')
  })

  it('can explicitly clear segment identity when a handoff changes basin version', () => {
    const state = parseM11QueryState(
      'source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-a&riverNetworkVersionId=rn-a&segmentId=seg-a&q=mainstem',
    )

    expect(m11QueryHref('/basins/basin-b', state, { basinVersionId: 'bv-b', riverNetworkVersionId: null, segmentId: null })).toBe(
      '/basins/basin-b?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-b&q=mainstem',
    )
  })
})
