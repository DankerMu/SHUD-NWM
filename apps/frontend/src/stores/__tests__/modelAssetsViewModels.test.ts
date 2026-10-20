import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  buildModelAssetDependencyGraph,
  buildModelAssetKpis,
  buildModelAssetMapProjection,
  buildModelAssetProducts,
  buildModelAssetTree,
  MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
  MODEL_ASSET_MAP_UNAVAILABLE_TEXT,
  MODEL_ASSET_PRODUCT_DISPLAY_LIMIT,
  MODEL_ASSET_PRODUCT_LIMIT_TEXT,
  MODEL_ASSET_UNAVAILABLE,
} from '@/stores/modelAssets'

import { BASINS_MODEL_ID, makeBasinsModel, resetStore } from './modelAssetsFixtures'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}))

describe('useModelAssetsStore', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetStore()
  })

  it('builds active/inactive/all tree filters, search no-results, excluded selection, and URL restoration state', () => {
    const activeModel = makeBasinsModel({ active_flag: true, model_id: BASINS_MODEL_ID, basin_name: 'QHH' })
    const inactiveModel = makeBasinsModel({
      active_flag: false,
      model_id: 'basins_heihe_shud',
      basin_id: 'basins_heihe',
      basin_name: 'Heihe',
      basin_version_id: 'heihe-version',
    })
    expect(buildModelAssetTree([activeModel, inactiveModel], { active: 'all' }).groups.flatMap((g) => g.models)).toHaveLength(2)
    expect(buildModelAssetTree([activeModel, inactiveModel], { active: 'true' }).groups.flatMap((g) => g.models).map((m) => m.model_id)).toEqual([
      BASINS_MODEL_ID,
    ])
    expect(buildModelAssetTree([activeModel, inactiveModel], { active: 'false' }).groups.flatMap((g) => g.models).map((m) => m.model_id)).toEqual([
      'basins_heihe_shud',
    ])
    expect(buildModelAssetTree([activeModel, inactiveModel], { search: 'missing' }).emptyMessage).toBe('无匹配模型')
    expect(buildModelAssetTree([activeModel, inactiveModel], { search: 'heihe', selectedModelId: BASINS_MODEL_ID })).toMatchObject({
      selectedInFilter: false,
    })
    expect(buildModelAssetTree([activeModel, inactiveModel], { search: 'qhh', selectedModelId: BASINS_MODEL_ID })).toMatchObject({
      selectedInFilter: true,
    })
  })

  it('builds the six KPI cards in the required order and renders missing values as unavailable', () => {
    const complete = buildModelAssetKpis(makeBasinsModel({ resource_profile: { ...makeBasinsModel().resource_profile, area_km2: 123.4 } }))
    expect(complete.map((kpi) => kpi.label)).toEqual([
      '流域版本',
      '河网版本',
      '网格版本',
      '率定版本',
      'SHUD / 模型',
      '河段 / 面积',
    ])
    expect(complete[5].value).toBe('42 河段 / 123.4 km²')

    const missing = buildModelAssetKpis(
      makeBasinsModel({
        mesh_checksum: null,
        segment_count: null,
        resource_profile: {},
      }),
    )
    expect(missing[5].value).toBe(MODEL_ASSET_UNAVAILABLE)
  })

  it('marks partial dependency graph nodes missing without inventing relationships', () => {
    const graph = buildModelAssetDependencyGraph(
      makeBasinsModel({
        mesh_version_id: '',
        calibration_version_id: '',
        package_checksum: null,
        model_package_uri: '',
        source_uri: null,
        resource_profile: { source_lineage: { source_path: '/volume/data/nwm/Basins/qhh' } },
      }),
    )

    expect(graph.nodes.find((node) => node.id === 'mesh')).toMatchObject({ missing: true, value: '暂不可用' })
    expect(graph.nodes.find((node) => node.id === 'calibration')).toMatchObject({ missing: true, value: '暂不可用' })
    expect(graph.nodes.find((node) => node.id === 'source')).toMatchObject({ missing: false, value: '受限来源' })
    expect(graph.edges).not.toContainEqual({ from: 'model', to: 'mesh' })
    expect(graph.edges).not.toContainEqual({ from: 'model', to: 'calibration' })
  })

  it('bounds product assets to 12 rows with redacted targets', () => {
    const products = Array.from({ length: 13 }, (_, index) => ({
      id: `asset-${String(index + 1).padStart(2, '0')}`,
      label: `Asset ${index + 1}`,
      checksum: `sha-${index + 1}`,
      uri: index === 0 ? 'https://user:pass@assets.example.test/pkg?token=abc#frag' : `s3://nhms/private/${index}`,
    }))
    const projection = buildModelAssetProducts(makeBasinsModel({ resource_profile: { product_assets: products } }))

    expect(projection.items).toHaveLength(12)
    expect(projection.notice).toBe(MODEL_ASSET_PRODUCT_LIMIT_TEXT)
    expect(projection.items[0]).toMatchObject({
      id: 'asset-01',
      checksum: 'sha-1',
      target: 'https://assets.example.test/pkg',
    })
  })

  it('does not access product entries beyond the display budget while detecting truncation', () => {
    const products = Array.from({ length: 10_000 }, (_, index) => ({
      id: `asset-${index + 1}`,
      label: `Asset ${index + 1}`,
      checksum: `sha-${index + 1}`,
      uri: `s3://nhms/private/${index + 1}`,
    }))
    Object.defineProperty(products, MODEL_ASSET_PRODUCT_DISPLAY_LIMIT, {
      get() {
        throw new Error('budget sentinel product was accessed')
      },
    })

    const projection = buildModelAssetProducts(makeBasinsModel({ resource_profile: { product_assets: products } }))

    expect(projection.items).toHaveLength(12)
    expect(projection.notice).toBe(MODEL_ASSET_PRODUCT_LIMIT_TEXT)
  })

  it.each(['product_assets', 'products', 'assets'] as const)(
    'bounds inspected %s entries when malformed prefixes precede valid products',
    (key) => {
      const products = Array.from({ length: 100_000 }, () => null) as unknown[]
      products.push({
        id: 'late-valid-product',
        label: 'Late Valid Product',
        checksum: 'late-sha',
        uri: 's3://nhms/private/late',
      })
      Object.defineProperty(products, 101, {
        get() {
          throw new Error('product inspection sentinel was accessed')
        },
      })

      const projection = buildModelAssetProducts(makeBasinsModel({ resource_profile: { [key]: products } }))

      expect(projection).toMatchObject({
        items: [],
        truncated: true,
        notice: MODEL_ASSET_PRODUCT_LIMIT_TEXT,
      })
    },
  )

  it('applies mini map geometry budgets and degraded states', () => {
    expect(buildModelAssetMapProjection(makeBasinsModel({ resource_profile: {} })).text).toBe(MODEL_ASSET_MAP_UNAVAILABLE_TEXT)

    const featureCollection = {
      type: 'FeatureCollection',
      features: Array.from({ length: 51 }, (_, index) => ({
        type: 'Feature',
        properties: { id: index },
        geometry: { type: 'Point', coordinates: [100 + index, 30] },
      })),
    }
    expect(buildModelAssetMapProjection(makeBasinsModel({ resource_profile: { geometry: featureCollection } })).text).toBe(
      MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
    )

    const longLine = {
      type: 'LineString',
      coordinates: Array.from({ length: 2001 }, (_, index) => [100 + index / 1000, 30]),
    }
    expect(buildModelAssetMapProjection(makeBasinsModel({ resource_profile: { geometry: longLine } })).text).toBe(
      MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
    )

    const riverGeometries = Array.from({ length: 51 }, (_, index) => ({
      type: 'Feature',
      properties: { id: index },
      geometry: { type: 'LineString', coordinates: [[100 + index / 1000, 30], [101 + index / 1000, 31]] },
    }))
    const riverProjection = buildModelAssetMapProjection(makeBasinsModel({ resource_profile: { river_geometries: riverGeometries } }))
    expect(riverProjection).toMatchObject({
      status: 'over-budget',
      text: MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
      geometry: null,
    })

    const geometryCollection = {
      type: 'GeometryCollection',
      geometries: [
        { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
        { type: 'Polygon', coordinates: [[[100, 30], [101, 30], [101, 31], [100, 30]]] },
      ],
    }
    expect(buildModelAssetMapProjection(makeBasinsModel({ resource_profile: { boundary: geometryCollection } }))).toMatchObject({
      status: 'available',
      featureCount: 2,
      vertexCount: 6,
      geometry: geometryCollection,
    })

    for (const malformed of [
      { type: 'FeatureCollection' },
      { type: 'Feature', geometry: null },
      { type: 'LineString' },
      { type: 'LineString', coordinates: [] },
    ]) {
      expect(buildModelAssetMapProjection(makeBasinsModel({ resource_profile: { basin_boundary: malformed } }))).toMatchObject({
        status: 'missing',
        text: MODEL_ASSET_MAP_UNAVAILABLE_TEXT,
        geometry: null,
      })
    }
  })
})
