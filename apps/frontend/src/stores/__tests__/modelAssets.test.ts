import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import {
  buildModelAssetDependencyGraph,
  buildModelAssetKpis,
  buildModelAssetMapProjection,
  buildModelAssetProducts,
  buildModelAssetTree,
  displaySanitizedSource,
  MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
  MODEL_ASSET_MAP_UNAVAILABLE_TEXT,
  MODEL_ASSET_PRODUCT_LIMIT_TEXT,
  MODEL_ASSET_RESTRICTED_SOURCE,
  MODEL_ASSET_UNAVAILABLE,
  sanitizeModelAssetString,
  type ModelAsset,
  type ModelAssetPage,
  useModelAssetsStore,
} from '@/stores/modelAssets'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

function failure(message: string) {
  return { data: undefined, error: { error: { message } } }
}

const BASINS_MODEL_ID = 'basins_basin_a_shud'
const MANIFEST_URI = 's3://nhms/models/basins_basin_a_shud/vbasins/manifest.json'
const PACKAGE_URI = 's3://nhms/models/basins_basin_a_shud/vbasins/package/'

function makeBasinsModel(overrides: Partial<ModelAsset> = {}): ModelAsset {
  return {
    model_id: BASINS_MODEL_ID,
    model_name: 'alias-a',
    basin_id: 'basins_basin_a',
    basin_name: 'Basin A',
    basin_version_id: 'basins_basin_a_vbasins',
    river_network_version_id: 'basins_basin_a_rivnet_vbasins',
    mesh_version_id: 'basins_basin_a_mesh_vbasins',
    calibration_version_id: 'basins_basin_a_shud_calib_vbasins',
    segment_count: 42,
    mesh_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh',
    mesh_checksum: 'mesh-sha-1',
    shud_code_version: 'basins-shud',
    active_flag: false,
    model_package_uri: PACKAGE_URI,
    package_checksum: 'package-sha-1',
    manifest_uri: MANIFEST_URI,
    source_inventory_checksum: 'inventory-sha-1',
    basin_slug: 'basin-a',
    shud_input_name: 'alias-a',
    source_path: '/volume/data/nwm/Basins/basin-a',
    resolved_source_path: '/volume/data/nwm/Basins/basin-a',
    source_uri: 's3://nhms/sources/basin-a',
    source_is_symlink: false,
    resource_profile: {
      basin_slug: 'basin-a',
      shud_input_name: 'alias-a',
      manifest_uri: MANIFEST_URI,
      package_checksum: 'package-sha-1',
      source_inventory_checksum: 'inventory-sha-1',
      segment_count: 42,
      mesh: {
        uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh',
        checksum: 'mesh-sha-1',
      },
      source_lineage: {
        source_path: '/volume/data/nwm/Basins/basin-a',
        source_uri: 's3://nhms/sources/basin-a',
      },
    },
    created_at: '2026-05-14T00:00:00Z',
    ...overrides,
  }
}

function resetStore() {
  useModelAssetsStore.setState(useModelAssetsStore.getInitialState(), true)
}

describe('useModelAssetsStore', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetStore()
  })

  it('loads Basins-backed model list metadata through the OpenAPI client', async () => {
    const model = makeBasinsModel()
    const page: ModelAssetPage = { items: [model], total: 1, limit: 25, offset: 0 }
    let query: Record<string, unknown> | undefined

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      if (path !== '/api/v1/models') throw new Error(`Unexpected GET ${path}`)
      query = options?.params?.query
      return success(page) as never
    })

    await useModelAssetsStore.getState().fetchModels({
      basinVersionId: 'basins_basin_a_vbasins',
      active: 'all',
      limit: 25,
      offset: 0,
    })

    const state = useModelAssetsStore.getState()
    expect(query).toMatchObject({
      basin_version_id: 'basins_basin_a_vbasins',
      active: 'all',
      limit: 25,
      offset: 0,
    })
    expect(state.models).toHaveLength(1)
    expect(state.models[0]).toMatchObject({
      model_id: BASINS_MODEL_ID,
      active_flag: false,
      segment_count: 42,
      mesh_checksum: 'mesh-sha-1',
      model_package_uri: PACKAGE_URI,
      package_checksum: 'package-sha-1',
      manifest_uri: MANIFEST_URI,
      source_inventory_checksum: 'inventory-sha-1',
      basin_slug: 'basin-a',
      shud_input_name: 'alias-a',
      source_path: null,
      resolved_source_path: null,
      source_uri: 's3://nhms/sources/basin-a',
      source_is_symlink: false,
    })
    expect(state.models[0].resource_profile).toMatchObject({
      basin_slug: 'basin-a',
      shud_input_name: 'alias-a',
      package_checksum: 'package-sha-1',
      source_inventory_checksum: 'inventory-sha-1',
      mesh: { checksum: 'mesh-sha-1' },
      source_lineage: {
        source_path: null,
        source_uri: 's3://nhms/sources/basin-a',
      },
    })
    expect(state.total).toBe(1)
    expect(state.loading).toBe(false)
    expect(state.error).toBeNull()
  })

  it('loads selected Basins model detail without placeholder-only type patches', async () => {
    const detail = makeBasinsModel({
      active_flag: true,
      segment_count: 108,
      resource_profile: {
        ...makeBasinsModel().resource_profile,
        active_source: 'operator_activation',
        segment_count: 108,
      },
    })
    let pathParams: Record<string, unknown> | undefined

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { path?: Record<string, unknown> } } | undefined
      if (path !== '/api/v1/models/{model_id}') throw new Error(`Unexpected GET ${path}`)
      pathParams = options?.params?.path
      return success(detail) as never
    })

    await useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)

    const selected = useModelAssetsStore.getState().selectedModel
    expect(pathParams).toEqual({ model_id: BASINS_MODEL_ID })
    expect(selected).toMatchObject({
      model_id: BASINS_MODEL_ID,
      active_flag: true,
      segment_count: 108,
      mesh_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh',
      mesh_checksum: 'mesh-sha-1',
      package_checksum: 'package-sha-1',
      manifest_uri: MANIFEST_URI,
      source_path: null,
      resolved_source_path: null,
      source_uri: 's3://nhms/sources/basin-a',
    })
    expect(selected?.resource_profile).toMatchObject({
      active_source: 'operator_activation',
      segment_count: 108,
      source_lineage: {
        source_path: null,
        source_uri: 's3://nhms/sources/basin-a',
      },
    })
    expect(useModelAssetsStore.getState().detailLoading).toBe(false)
    expect(useModelAssetsStore.getState().error).toBeNull()
  })

  it('preserves URI-like source paths while redacting local resolved paths', async () => {
    const model = makeBasinsModel({
      source_path: 's3://key:secret@nhms/raw/basin-a?sig=x#frag',
      resolved_source_path: 'https://user:pass@assets.example.test/basin-a?token=abc#frag',
      resource_profile: {
        ...makeBasinsModel().resource_profile,
        source_lineage: {
          source_path: 's3://key:secret@nhms/raw/basin-a?sig=x#frag',
          resolved_source_path: '/volume/data/nwm/Basins/basin-a',
          source_uri: 'https://user:pass@assets.example.test/pkg?token=abc#frag',
        },
      },
    })
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }

    vi.mocked(client.GET).mockResolvedValue(success(page) as never)

    await useModelAssetsStore.getState().fetchModels()

    expect(useModelAssetsStore.getState().models[0]).toMatchObject({
      source_path: 's3://nhms/raw/basin-a',
      resolved_source_path: 'https://assets.example.test/basin-a',
    })
    expect(useModelAssetsStore.getState().models[0].resource_profile).toMatchObject({
      source_lineage: {
        source_path: 's3://nhms/raw/basin-a',
        resolved_source_path: null,
        source_uri: 'https://assets.example.test/pkg',
      },
    })
  })

  it('redacts local absolute paths, file URIs, URI userinfo, query, and fragment', async () => {
    expect(sanitizeModelAssetString('/volume/data/nwm/Basins/qhh')).toBeNull()
    expect(sanitizeModelAssetString('C:\\nwm\\Basins\\qhh')).toBeNull()
    expect(sanitizeModelAssetString('file:///volume/data/nwm/Basins/qhh')).toBeNull()
    expect(sanitizeModelAssetString('https://user:pass@assets.example.test/pkg?token=abc#frag')).toBe(
      'https://assets.example.test/pkg',
    )
    expect(sanitizeModelAssetString('s3://key:secret@nhms/private/package?sig=x#frag')).toBe(
      's3://nhms/private/package',
    )
    expect(displaySanitizedSource('/volume/data/nwm/Basins/qhh')).toBe('受限来源')
  })

  it('preserves restricted source semantics after store normalization without retaining raw local strings', async () => {
    const model = makeBasinsModel({
      source_path: '/volume/data/nwm/Basins/qhh',
      resolved_source_path: 'C:\\nwm\\Basins\\qhh',
      source_uri: 'file:///volume/data/nwm/Basins/qhh?token=abc#frag',
      resource_profile: {
        source_lineage: {
          source_path: '/volume/data/nwm/Basins/qhh',
          source_uri: 'file:///volume/data/nwm/Basins/qhh?token=abc#frag',
        },
        product_assets: [
          {
            id: 'restricted-package',
            label: 'Restricted Package',
            checksum: 'sha-restricted',
            uri: '/volume/data/nwm/Basins/qhh/package.zip',
          },
        ],
      },
    })
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }

    vi.mocked(client.GET).mockResolvedValue(success(page) as never)

    await useModelAssetsStore.getState().fetchModels()

    const normalized = useModelAssetsStore.getState().models[0]
    expect(normalized).toMatchObject({
      source_path: null,
      resolved_source_path: null,
      source_uri: null,
      resource_profile: {
        source_lineage: {
          source_path: null,
          source_uri: null,
        },
      },
    })
    expect(JSON.stringify(normalized)).not.toContain('/volume/data')
    expect(JSON.stringify(normalized)).not.toContain('C:\\nwm')
    expect(JSON.stringify(normalized)).not.toContain('file://')
    expect(JSON.stringify(normalized)).not.toContain('token=abc')
    expect(JSON.stringify(normalized)).not.toContain('#frag')
    expect(buildModelAssetDependencyGraph(normalized).nodes.find((node) => node.id === 'source')).toMatchObject({
      missing: false,
      value: MODEL_ASSET_RESTRICTED_SOURCE,
    })
    expect(buildModelAssetProducts(normalized).items[0]).toMatchObject({
      id: 'restricted-package',
      target: MODEL_ASSET_RESTRICTED_SOURCE,
    })
  })

  it('clears stale selected model when the next detail request fails', async () => {
    const detail = makeBasinsModel()

    vi.mocked(client.GET)
      .mockResolvedValueOnce(success(detail) as never)
      .mockResolvedValueOnce(failure('model detail unavailable') as never)

    await useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)
    expect(useModelAssetsStore.getState().selectedModel?.model_id).toBe(BASINS_MODEL_ID)

    await expect(useModelAssetsStore.getState().fetchModelDetail('missing-model')).rejects.toThrow(
      'model detail unavailable',
    )

    expect(useModelAssetsStore.getState()).toMatchObject({
      selectedModel: null,
      detailLoading: false,
      error: 'model detail unavailable',
    })
  })

  it('keeps API errors in state for asset-management callers', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure('model registry unavailable') as never)

    await expect(useModelAssetsStore.getState().fetchModels()).rejects.toThrow('model registry unavailable')

    expect(useModelAssetsStore.getState()).toMatchObject({
      loading: false,
      error: 'model registry unavailable',
    })
  })

  it('keeps an empty registry and clears stale selected detail after list reload', async () => {
    vi.mocked(client.GET)
      .mockResolvedValueOnce(success(makeBasinsModel()) as never)
      .mockResolvedValueOnce(success({ items: [], total: 0, limit: 50, offset: 0 }) as never)

    await useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)
    expect(useModelAssetsStore.getState().selectedModel?.model_id).toBe(BASINS_MODEL_ID)

    await useModelAssetsStore.getState().fetchModels()

    expect(useModelAssetsStore.getState().models).toEqual([])
    expect(useModelAssetsStore.getState().selectedModel).toBeNull()
    expect(buildModelAssetTree(useModelAssetsStore.getState().models).emptyMessage).toBe('暂无模型资产')
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
  })
})
