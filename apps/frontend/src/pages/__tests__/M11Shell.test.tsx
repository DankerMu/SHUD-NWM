import { describe, expect, it } from 'vitest'

import {
  buildBasinFeatureCollection,
  buildBasinRiverFeatureCollection,
  buildM11RegisteredOverlay,
  countSkippedBasinGeometries,
  m11BasinBoundaryOverlayEnabled,
  m11VectorSourceKey,
} from '@/components/map/M11MapLibreSurface'
import type { BasinSegmentRow, LayerState, OverviewBasin } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import type { M11QueryState } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'

const state: M11QueryState = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
}

// 全国 source/cycle 形状（spec mvt-tile-contract Requirement「Frontend M11Shell mock fixture
// mirrors canonical discharge shape」）：模板含 `/hydro-national/{source}/{cycle}/`、六元组占位符、
// `source_refs` 无 run_id、`min_zoom` 等于后端 `_NATIONAL_DISCHARGE_METADATA.min_zoom`（3）、
// 并带 `default_source` / `default_cycle`。valid_times 用后端的秒精度拼写。
const dischargeMetadata = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  maplibre_source_layer: 'hydro',
  min_zoom: 3,
  max_zoom: 10,
  valid_times: ['2026-05-18T06:00:00Z'],
  url_template: '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  required_placeholders: ['source', 'cycle', 'valid_time', 'z', 'x', 'y'],
  source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
  default_source: 'gfs',
  default_cycle: '2026-05-18T00:00:00Z',
} as never

/**
 * `buildMvtTileUrlTemplate` 对每个替换值做 `encodeURIComponent`（仓库既有约定，与现役单 run 瓦片
 * 路径同源，服务端解码后再路由），所以线上路径段是 `2026-05-18T00%3A00%3A00Z`。
 * spec 给的字面 URL 是**解码后**的路径 —— 断言按解码口径比对，不去改这个被
 * `M11MapLibreSurface` 共用的编码器（fixture 决策 6）。
 */
function decodedTilePath(tileUrl: string): string {
  return decodeURIComponent(new URL(tileUrl, 'http://localhost').pathname)
}

const dischargeLayer: LayerState = {
  layerId: 'discharge',
  displayName: 'Discharge',
  group: 'hydrology',
  available: true,
  metadata: dischargeMetadata,
  validTimes: ['2026-05-18T06:00:00Z'],
  currentValidTime: '2026-05-18T06:00:00Z',
  validTimeSource: 'api',
  disabledReason: null,
  freshness: {
    updatedAt: null,
    cycleTime: state.cycle,
    validTime: state.validTime,
    runId: 'run-001',
    basinVersionId: 'bv-001',
    riverNetworkVersionId: 'rn-001',
    source: 'GFS',
    isStale: false,
    staleAfterHours: 6,
    unavailableReason: null,
  },
  legend: [],
}

const basinSegment: BasinSegmentRow = {
  riverSegmentId: 'river-001',
  riverNetworkVersionId: 'rn-001',
  segmentId: 'seg-001',
  displayName: 'Demo River',
  basinVersionId: 'bv-001',
  streamOrder: 2,
  lengthM: 1000,
  currentQ: 25,
  qUnit: 'm3/s',
  source: 'GFS',
  cycleTime: state.cycle,
  validTime: state.validTime,
  hasGeometry: true,
  geometry: {
    type: 'LineString',
    coordinates: [
      [100, 30],
      [101, 31],
    ],
  },
  unavailableReason: null,
}

describe('M11 discharge shell contracts', () => {
  it('pins the default-discharge fixture to the canonical national shape', () => {
    // spec mvt-tile-contract scenario「M11Shell unit-test default-discharge fixture uses national shape」
    const metadata = dischargeMetadata as unknown as {
      url_template: string
      required_placeholders: string[]
      source_refs: Record<string, unknown>
      min_zoom: number
    }

    expect(metadata.url_template).toContain('/api/v1/tiles/hydro-national/{source}/{cycle}/')
    expect(metadata.url_template).not.toContain('{run_id}')
    expect(metadata.required_placeholders).toEqual(['source', 'cycle', 'valid_time', 'z', 'x', 'y'])
    expect(Object.keys(metadata.source_refs)).not.toContain('run_id')
    expect(metadata.min_zoom).toBe(3)
  })

  it('registers the discharge vector overlay with q_down variable identity', () => {
    const overlay = buildM11RegisteredOverlay(state, [dischargeLayer])

    expect(overlay).not.toBeNull()
    expect(overlay?.layerId).toBe('discharge')
    // 旧断言是 `/api/v1/tiles/hydro/`（单 run 模板），随 fixture 一起改为全国 source/cycle 路径
    // （spec scenario「Existing assertions against the legacy single-run URL are updated with the fixture」）。
    expect(decodedTilePath(overlay?.source.tiles[0] as string)).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-18T00:00:00Z/q_down/2026-05-18T06:00:00Z/{z}/{x}/{y}.pbf',
    )
    expect(decodedTilePath(overlay?.source.tiles[0] as string)).toContain('/q_down/')
    expect(overlay?.source.minzoom).toBe(3)
    expect(JSON.stringify(overlay?.layer.paint)).toContain('value')
  })

  it('substitutes the spec source/cycle/validTime triple into the national template', () => {
    // spec frontend-mvt-layer-consumption scenario「National template substitution」的字面 URL。
    const ifsState: M11QueryState = {
      ...state,
      source: 'ifs',
      cycle: '2026-09-02T12:00:00Z',
      validTime: '2026-09-02T15:00:00Z',
    }
    const overlay = buildM11RegisteredOverlay(ifsState, [
      { ...dischargeLayer, validTimes: ['2026-09-02T15:00:00Z'], currentValidTime: '2026-09-02T15:00:00Z' },
    ])

    expect(decodedTilePath(overlay?.source.tiles[0] as string)).toBe(
      '/api/v1/tiles/hydro-national/ifs/2026-09-02T12:00:00Z/q_down/2026-09-02T15:00:00Z/{z}/{x}/{y}.pbf',
    )
    // 编码行为不变：线上路径段仍是 %3A（决策 6）。
    expect(overlay?.source.tiles[0]).toContain('2026-09-02T12%3A00%3A00Z')
  })

  it('canonicalizes millisecond query instants to the seconds spelling in URL and list matching', () => {
    // spec scenario「Substituted instants are canonicalized to seconds precision」：state 是 `.000Z`，
    // API 列表是 `…:00Z`，两边都必须按秒精度比对与拼接。
    const millisecondState: M11QueryState = {
      ...state,
      cycle: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
    }
    const overlay = buildM11RegisteredOverlay(millisecondState, [dischargeLayer])

    const path = decodedTilePath(overlay?.source.tiles[0] as string)
    expect(path).toContain('/gfs/2026-05-18T00:00:00Z/q_down/2026-05-18T06:00:00Z/')
    expect(path).not.toContain('.000Z')
    expect(JSON.parse(overlay?.sourceKey as string)).toMatchObject({
      cycle: '2026-05-18T00:00:00Z',
      valid_time: '2026-05-18T06:00:00Z',
    })
  })

  it('renders a non-default cycle from the store-held per-cycle list', () => {
    // spec scenario「Non-default cycle fetches its own list」：目录 metadata.valid_times 只带默认周期，
    // overlay 必须用 LayerState 里活动 (source, cycle) 的列表校验，而不是 metadata.valid_times。
    const nonDefault: M11QueryState = {
      ...state,
      cycle: '2026-05-18T12:00:00Z',
      validTime: '2026-05-18T18:00:00Z',
    }
    const overlay = buildM11RegisteredOverlay(nonDefault, [
      { ...dischargeLayer, validTimes: ['2026-05-18T18:00:00Z'], currentValidTime: '2026-05-18T18:00:00Z' },
    ])

    expect(overlay).not.toBeNull()
    expect(decodedTilePath(overlay?.source.tiles[0] as string)).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-18T12:00:00Z/q_down/2026-05-18T18:00:00Z/{z}/{x}/{y}.pbf',
    )
  })

  it('registers no overlay and no literal {cycle} when the catalog advertises no default cycle', () => {
    // spec map-layer-timeline-controls「Cycle selector is fail-closed」：default_cycle 为 null 时，
    // 无论 URL 上有没有 cycle 都不注册叠加层，也就不会请求含字面 {cycle} 的瓦片。
    const failClosedLayer: LayerState = {
      ...dischargeLayer,
      metadata: { ...(dischargeMetadata as object), default_cycle: null } as never,
    }

    expect(buildM11RegisteredOverlay(state, [failClosedLayer])).toBeNull()
    expect(buildM11RegisteredOverlay({ ...state, cycle: null }, [failClosedLayer])).toBeNull()
  })

  it('uses a vector source key that varies with each of source, cycle and validTime', () => {
    // spec mvt-tile-contract：`m11VectorSourceKey` 必须区分 (source, cycle, valid_time) 而不是 run_id，
    // 否则切周期时 MapLibre 复用旧 source，地图静默显示上一周期数据。
    const base = {
      layerId: 'discharge',
      runId: null,
      source: 'gfs',
      cycle: '2026-05-18T00:00:00Z',
      validTime: '2026-05-18T06:00:00Z',
      variable: 'q_down',
      metadata: dischargeMetadata,
    }
    const key = m11VectorSourceKey(base)

    expect(JSON.parse(key)).toMatchObject({
      layer_id: 'discharge',
      run_id: null,
      source: 'gfs',
      cycle: '2026-05-18T00:00:00Z',
      valid_time: '2026-05-18T06:00:00Z',
      variable: 'q_down',
      maplibre_source_layer: 'hydro',
    })
    expect(m11VectorSourceKey({ ...base, source: 'ifs' })).not.toBe(key)
    expect(m11VectorSourceKey({ ...base, cycle: '2026-05-18T12:00:00Z' })).not.toBe(key)
    expect(m11VectorSourceKey({ ...base, validTime: '2026-05-18T09:00:00Z' })).not.toBe(key)
  })

  it('builds basin river feature properties from discharge rows only', () => {
    const collection = buildBasinRiverFeatureCollection([basinSegment], 'discharge')

    expect(collection.features).toHaveLength(1)
    expect(collection.features[0].properties).toMatchObject({
      river_segment_id: 'river-001',
      q_value: 25,
      q_unit: 'm3/s',
    })
  })

  it('suppresses every basin boundary and its map label source', () => {
    const basins: OverviewBasin[] = [
      {
        basinId: 'basins_qhh',
        displayName: 'QHH',
        basinGroup: null,
        parentBasinId: null,
        level: 1,
        qualityNote: null,
        areaKm2: null,
        riverCount: null,
        activeModelCount: 0,
        latestForecastTime: null,
        selectedBasinVersionId: null,
        basinVersions: [],
        boundary: {
          type: 'MultiPolygon',
          coordinates: [[[[98, 37], [99, 37], [99, 38], [98, 37]]]],
        },
        bbox: { minLon: 98, minLat: 37, maxLon: 99, maxLat: 38 },
        unavailableReason: null,
      },
    ]

    expect(m11BasinBoundaryOverlayEnabled).toBe(false)
    expect(buildBasinFeatureCollection(basins, undefined).features).toEqual([])
    expect(countSkippedBasinGeometries(basins, undefined)).toBe(0)
  })

  it('keeps shared visual tokens available for the overview surface', () => {
    expect(m11VisualTokens.navHeight).toBe('0px')
  })
})
