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
  MODEL_ASSET_PRODUCT_DISPLAY_LIMIT,
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
    POST: vi.fn(),
  },
}))

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

function failure(message: string) {
  return { data: undefined, error: { error: { message } } }
}

const UNSAFE_MODEL_ASSET_ERROR =
  'failed to inspect /volume/data/nwm/Basins/qhh and C:\\nwm\\Basins\\qhh from file:///volume/data/nwm/Basins/qhh?token=abc#frag via https://user:pass@assets.example.test/pkg?token=abc#frag'
const UNSAFE_MODEL_ASSET_ERROR_TOKENS = [
  '/volume/data/nwm/Basins/qhh',
  'C:\\nwm\\Basins\\qhh',
  'file://',
  'user:pass',
  'token=abc',
  '#frag',
] as const

function expectNoUnsafeModelAssetErrorText(value: string | null) {
  expect(value).toBeTruthy()
  for (const token of UNSAFE_MODEL_ASSET_ERROR_TOKENS) {
    expect(value).not.toContain(token)
  }
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
    expect(sanitizeModelAssetString('/tmp/nhms/private/model-root')).toBeNull()
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
      model_package_uri: '/volume/data/nwm/Basins/qhh/package.zip',
      manifest_uri: 'file:///volume/data/nwm/Basins/qhh/manifest.json',
      mesh_uri: 'C:\\nwm\\Basins\\qhh\\mesh.sp',
      resource_profile: {
        source_path: '/volume/data/nwm/Basins/qhh/profile-source',
        source_lineage: {
          source_path: '/volume/data/nwm/Basins/qhh',
          local_path: '/volume/data/nwm/Basins/qhh/local',
          source_uri: 'file:///volume/data/nwm/Basins/qhh?token=abc#frag',
          uris: ['file:///volume/data/nwm/Basins/qhh/lineage', 's3://nhms/safe/fallback'],
        },
        product_assets: [
          {
            id: 'restricted-package',
            label: 'Restricted Package',
            checksum: 'sha-restricted',
            uri: '/volume/data/nwm/Basins/qhh/package.zip',
          },
          {
            id: 'restricted-path-product',
            label: 'Restricted Path Product',
            checksum: 'sha-restricted-path',
            path: 'file:///volume/data/nwm/Basins/qhh/product.bin',
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
      model_package_uri: null,
      manifest_uri: null,
      mesh_uri: null,
      resource_profile: {
        source_path: null,
        source_lineage: {
          source_path: null,
          local_path: null,
          source_uri: null,
          uris: [null, 's3://nhms/safe/fallback'],
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
    const productProjection = buildModelAssetProducts(normalized)
    expect(productProjection.items[0]).toMatchObject({
      id: 'restricted-package',
      target: MODEL_ASSET_RESTRICTED_SOURCE,
    })
    expect(productProjection.items[1]).toMatchObject({
      id: 'restricted-path-product',
      target: MODEL_ASSET_RESTRICTED_SOURCE,
    })
  })

  it('marks nested local source aliases as restricted on lineage graph surfaces', async () => {
    const model = makeBasinsModel({
      source_path: null,
      source_uri: null,
      resource_profile: {
        source_path: '/volume/data/nwm/Basins/qhh/profile-source',
        source_lineage: {
          local_path: '/volume/data/nwm/Basins/qhh/local',
          uris: ['file:///volume/data/nwm/Basins/qhh/lineage'],
        },
      },
    })
    vi.mocked(client.GET).mockResolvedValue(success({ items: [model], total: 1, limit: 50, offset: 0 }) as never)

    await useModelAssetsStore.getState().fetchModels()

    const normalized = useModelAssetsStore.getState().models[0]
    expect(JSON.stringify(normalized)).not.toContain('/volume/data')
    expect(JSON.stringify(normalized)).not.toContain('file://')
    expect(buildModelAssetDependencyGraph(normalized).nodes.find((node) => node.id === 'source')).toMatchObject({
      missing: false,
      value: MODEL_ASSET_RESTRICTED_SOURCE,
    })
  })

  it.each([
    [
      'top-level URI safe, nested URI restricted',
      {
        source_uri: 's3://nhms/safe/top-level',
        resource_profile: {
          source_lineage: {
            source_uri: 'file:///volume/data/nwm/Basins/qhh/restricted-uri',
          },
        },
      },
    ],
    [
      'top-level URI restricted, nested URI safe',
      {
        source_uri: 'file:///volume/data/nwm/Basins/qhh/restricted-uri',
        resource_profile: {
          source_lineage: {
            source_uri: 's3://nhms/safe/nested',
          },
        },
      },
    ],
  ])('keeps Source URI restricted-source precedence for %s', async (_caseName, overrides) => {
    vi.mocked(client.GET).mockResolvedValue(success({ items: [makeBasinsModel(overrides)], total: 1, limit: 50, offset: 0 }) as never)

    await useModelAssetsStore.getState().fetchModels()

    const normalized = useModelAssetsStore.getState().models[0]
    const serialized = JSON.stringify(normalized)
    expect(serialized).not.toContain('file:///volume/data')
    expect(buildModelAssetDependencyGraph(normalized).nodes.find((node) => node.id === 'source')).toMatchObject({
      missing: false,
      value: MODEL_ASSET_RESTRICTED_SOURCE,
    })
  })

  it.each([
    [
      'top-level path safe, nested path restricted',
      {
        source_path: 's3://nhms/safe/top-level-path',
        resource_profile: {
          source_lineage: {
            source_path: 'file:///volume/data/nwm/Basins/qhh/restricted-path',
          },
        },
      },
    ],
    [
      'top-level path restricted, nested path safe',
      {
        source_path: '/volume/data/nwm/Basins/qhh/restricted-path',
        resource_profile: {
          source_lineage: {
            source_path: 's3://nhms/safe/nested-path',
          },
        },
      },
    ],
    [
      'safe lineage path, restricted URI fallback',
      {
        source_path: null,
        resource_profile: {
          source_lineage: {
            source_path: 's3://nhms/safe/lineage-path',
            uris: ['file:///volume/data/nwm/Basins/qhh/restricted-uri-fallback'],
          },
        },
      },
    ],
  ])('keeps Source Path restricted-source precedence for %s', async (_caseName, overrides) => {
    vi.mocked(client.GET).mockResolvedValue(success({ items: [makeBasinsModel(overrides)], total: 1, limit: 50, offset: 0 }) as never)

    await useModelAssetsStore.getState().fetchModels()

    const normalized = useModelAssetsStore.getState().models[0]
    const serialized = JSON.stringify(normalized)
    expect(serialized).not.toContain('/volume/data')
    expect(buildModelAssetDependencyGraph(normalized).nodes.find((node) => node.id === 'source')).toMatchObject({
      missing: false,
      value: MODEL_ASSET_RESTRICTED_SOURCE,
    })
  })

  it('clears stale selected model when the next detail request fails', async () => {
    const detail = makeBasinsModel()

    vi.mocked(client.GET)
      .mockResolvedValueOnce(success(detail) as never)
      .mockResolvedValueOnce(failure('model detail unavailable') as never)

    await useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)
    expect(useModelAssetsStore.getState().selectedModel?.model_id).toBe(BASINS_MODEL_ID)

    await expect(useModelAssetsStore.getState().fetchModelDetail('missing-model')).rejects.toThrow('模型资产详情加载失败')

    expect(useModelAssetsStore.getState()).toMatchObject({
      selectedModel: null,
      detailLoading: false,
      error: '模型资产详情加载失败',
    })
  })

  it('ignores out-of-order stale detail responses and keeps the latest requested model selected', async () => {
    let resolveA: ((value: ReturnType<typeof success<ModelAsset>>) => void) | undefined
    let resolveB: ((value: ReturnType<typeof success<ModelAsset>>) => void) | undefined
    vi.mocked(client.GET).mockImplementation((async (_path: string, options?: { params?: { path?: { model_id?: string } } }) => {
      const modelId = options?.params?.path?.model_id
      if (modelId === 'model-a') {
        return await new Promise((resolve) => {
          resolveA = resolve
        })
      }
      if (modelId === 'model-b') {
        return await new Promise((resolve) => {
          resolveB = resolve
        })
      }
      throw new Error(`Unexpected model ${modelId}`)
    }) as never)

    const requestA = useModelAssetsStore.getState().fetchModelDetail('model-a')
    const requestB = useModelAssetsStore.getState().fetchModelDetail('model-b')

    resolveB?.(success(makeBasinsModel({ model_id: 'model-b', model_name: 'Model B' })))
    await requestB
    expect(useModelAssetsStore.getState().selectedModel?.model_id).toBe('model-b')

    resolveA?.(success(makeBasinsModel({ model_id: 'model-a', model_name: 'Model A' })))
    await requestA
    expect(useModelAssetsStore.getState().selectedModel?.model_id).toBe('model-b')
    expect(useModelAssetsStore.getState().detailLoading).toBe(false)
  })

  it('rejects a detail response whose body model id does not match the requested model', async () => {
    vi.mocked(client.GET).mockResolvedValue(success(makeBasinsModel({ model_id: 'model-a', model_name: 'Model A' })) as never)

    await expect(useModelAssetsStore.getState().fetchModelDetail('model-b')).rejects.toThrow(
      '模型资产详情与当前选择不匹配',
    )

    expect(useModelAssetsStore.getState()).toMatchObject({
      selectedModel: null,
      detailLoading: false,
      error: '模型资产详情与当前选择不匹配',
    })
  })

  it('stores only the safe generic list error when API failures include sensitive source strings', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure(UNSAFE_MODEL_ASSET_ERROR) as never)

    await expect(useModelAssetsStore.getState().fetchModels()).rejects.toThrow('模型资产列表加载失败')

    const error = useModelAssetsStore.getState().error
    expect(error).toBe('模型资产列表加载失败')
    expectNoUnsafeModelAssetErrorText(error)
  })

  it('stores only the safe generic detail error when API failures include sensitive source strings', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure(UNSAFE_MODEL_ASSET_ERROR) as never)

    await expect(useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)).rejects.toThrow('模型资产详情加载失败')

    const error = useModelAssetsStore.getState().error
    expect(error).toBe('模型资产详情加载失败')
    expectNoUnsafeModelAssetErrorText(error)
  })

  it('keeps safe generic API errors in state for asset-management callers', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure('model registry unavailable') as never)

    await expect(useModelAssetsStore.getState().fetchModels()).rejects.toThrow('模型资产列表加载失败')

    expect(useModelAssetsStore.getState()).toMatchObject({
      loading: false,
      error: '模型资产列表加载失败',
    })
  })

  it('clears stale selected detail when list loading fails', async () => {
    vi.mocked(client.GET)
      .mockResolvedValueOnce(success(makeBasinsModel()) as never)
      .mockResolvedValueOnce(failure('model registry unavailable') as never)

    await useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)
    expect(useModelAssetsStore.getState().selectedModel?.model_id).toBe(BASINS_MODEL_ID)

    await expect(useModelAssetsStore.getState().fetchModels()).rejects.toThrow('模型资产列表加载失败')

    expect(useModelAssetsStore.getState()).toMatchObject({
      selectedModel: null,
      detailLoading: false,
      loading: false,
      error: '模型资产列表加载失败',
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

  it.each(['fetchModels', 'fetchModelDetail'] as const)(
    'marks raw over-budget geometry loaded through %s as degraded after normalization',
    async (method) => {
      const overBudgetGeometry = {
        type: 'LineString',
        coordinates: Array.from({ length: 10_000 }, (_, index) => [100 + index / 10_000, 30]),
      }
      const model = makeBasinsModel({
        resource_profile: {
          geometry: overBudgetGeometry,
        },
      })
      vi.mocked(client.GET).mockResolvedValue(
        success(method === 'fetchModels' ? { items: [model], total: 1, limit: 50, offset: 0 } : model) as never,
      )

      if (method === 'fetchModels') {
        await useModelAssetsStore.getState().fetchModels()
      } else {
        await useModelAssetsStore.getState().fetchModelDetail(BASINS_MODEL_ID)
      }

      const normalized =
        method === 'fetchModels' ? useModelAssetsStore.getState().models[0] : useModelAssetsStore.getState().selectedModel
      const projection = buildModelAssetMapProjection(normalized)
      expect(JSON.stringify(normalized?.resource_profile)).not.toContain('109.9999')
      expect(projection).toMatchObject({
        status: 'over-budget',
        text: MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
        geometry: null,
      })
    },
  )

  it('bounds sanitizer recursion and wide resource profiles without leaking local strings', async () => {
    let deep: Record<string, unknown> = { source_path: '/volume/data/nwm/Basins/deep-secret' }
    for (let depth = 0; depth < 80; depth += 1) deep = { child: deep }
    const wide = Object.fromEntries(
      Array.from({ length: 6000 }, (_, index) => [`path_${index}`, `/volume/data/nwm/Basins/wide-secret-${index}`]),
    )
    const model = makeBasinsModel({
      source_path: null,
      source_uri: null,
      resource_profile: {
        source_lineage: {
          local_path: '/volume/data/nwm/Basins/root-secret',
        },
        deep,
        wide,
      },
    })
    vi.mocked(client.GET).mockResolvedValue(success({ items: [model], total: 1, limit: 50, offset: 0 }) as never)

    await useModelAssetsStore.getState().fetchModels()

    const normalized = useModelAssetsStore.getState().models[0]
    const serialized = JSON.stringify(normalized)
    expect(serialized).not.toContain('/volume/data')
    expect(serialized).not.toContain('root-secret')
    expect(serialized).not.toContain('deep-secret')
    expect(serialized).not.toContain('wide-secret')
    expect(buildModelAssetDependencyGraph(normalized).nodes.find((node) => node.id === 'source')).toMatchObject({
      value: MODEL_ASSET_RESTRICTED_SOURCE,
      missing: false,
    })
  })

  it('runs lifecycle preflight and successful operation through typed API paths', async () => {
    const model = makeBasinsModel({ active_flag: false, lifecycle_state: 'inactive' })
    const activeModel = makeBasinsModel({ active_flag: true, lifecycle_state: 'active' })
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'activate',
      status: 'ready',
      model_id: BASINS_MODEL_ID,
      basin_version_id: model.basin_version_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'allowed',
      operation: 'activate',
      model: activeModel,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: BASINS_MODEL_ID, log_id: 5 },
    }

    vi.mocked(client.POST).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/models/{model_id}/preflight') return success(preflight) as never
      if (path === '/api/v1/models/{model_id}/lifecycle') return success(result) as never
      throw new Error(`Unexpected POST ${path}`)
    })

    const preflightResponse = await useModelAssetsStore.getState().preflightModelOperation(BASINS_MODEL_ID, {
      operation: 'activate',
    })
    const resultResponse = await useModelAssetsStore.getState().runModelOperation(BASINS_MODEL_ID, {
      operation: 'activate',
    })

    expect(preflightResponse.status).toBe('ready')
    expect(resultResponse.audit_reference).toEqual({ entity_type: 'model_instance', entity_id: BASINS_MODEL_ID, log_id: 5 })
    expect(useModelAssetsStore.getState().operationResult?.status).toBe('allowed')
    expect(useModelAssetsStore.getState().operationPreflight?.status).toBe('ready')
  })

  it('forwards selected rollback previous model id through preflight and lifecycle calls', async () => {
    const outgoing = makeBasinsModel({ model_id: 'model-a', active_flag: true, lifecycle_state: 'active' })
    const staleSibling = makeBasinsModel({ model_id: 'model-b', active_flag: false, lifecycle_state: 'superseded' })
    const selectedPrior = makeBasinsModel({ model_id: 'model-c', active_flag: false, lifecycle_state: 'superseded' })
    const restored = makeBasinsModel({ ...selectedPrior, active_flag: true, lifecycle_state: 'active' })
    const demoted = makeBasinsModel({ ...outgoing, active_flag: false, lifecycle_state: 'superseded' })
    const postedBodies: unknown[] = []
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'rollback_version',
      status: 'ready',
      model_id: outgoing.model_id,
      basin_version_id: outgoing.basin_version_id,
      current_active_model_id: outgoing.model_id,
      previous_model_id: selectedPrior.model_id,
      restored_model_id: selectedPrior.model_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'rollback',
      operation: 'rollback_version',
      model: restored,
      previous_model: demoted,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: outgoing.model_id, log_id: 11 },
    }
    useModelAssetsStore.setState({
      models: [outgoing, staleSibling, selectedPrior],
      selectedModel: outgoing,
    })
    vi.mocked(client.POST).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const body = (args[1] as { body?: unknown } | undefined)?.body
      postedBodies.push(body)
      if (path === '/api/v1/models/{model_id}/preflight') return success(preflight) as never
      if (path === '/api/v1/models/{model_id}/lifecycle') return success(result) as never
      throw new Error(`Unexpected POST ${path}`)
    })

    await useModelAssetsStore.getState().preflightModelOperation(outgoing.model_id, {
      operation: 'rollback_version',
      previous_model_id: selectedPrior.model_id,
    })
    await useModelAssetsStore.getState().runModelOperation(outgoing.model_id, {
      operation: 'rollback_version',
      previous_model_id: selectedPrior.model_id,
    })

    expect(postedBodies).toEqual([
      { operation: 'rollback_version', previous_model_id: selectedPrior.model_id },
      { operation: 'rollback_version', previous_model_id: selectedPrior.model_id },
    ])
    expect(JSON.stringify(postedBodies)).not.toContain(staleSibling.model_id)
    expect(useModelAssetsStore.getState().operationPreflight?.restored_model_id).toBe(selectedPrior.model_id)
  })

  it('merges lifecycle result and previous model after activation', async () => {
    const previous = makeBasinsModel({ model_id: 'model-a', active_flag: true, lifecycle_state: 'active' })
    const candidate = makeBasinsModel({ model_id: 'model-b', active_flag: false, lifecycle_state: 'inactive' })
    const activated = makeBasinsModel({ ...candidate, active_flag: true, lifecycle_state: 'active' })
    const demoted = makeBasinsModel({ ...previous, active_flag: false, lifecycle_state: 'superseded' })
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'activate',
      status: 'ready',
      model_id: candidate.model_id,
      basin_version_id: candidate.basin_version_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'allowed',
      operation: 'activate',
      model: activated,
      previous_model: demoted,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: candidate.model_id, log_id: 9 },
    }

    useModelAssetsStore.setState({
      models: [previous, candidate],
      selectedModel: previous,
    })
    vi.mocked(client.POST).mockResolvedValue(success(result) as never)

    await useModelAssetsStore.getState().runModelOperation(candidate.model_id, { operation: 'activate' })

    const state = useModelAssetsStore.getState()
    expect(state.models.filter((model) => model.active_flag)).toHaveLength(1)
    expect(state.models.find((model) => model.model_id === candidate.model_id)).toMatchObject({
      active_flag: true,
      lifecycle_state: 'active',
    })
    expect(state.models.find((model) => model.model_id === previous.model_id)).toMatchObject({
      active_flag: false,
      lifecycle_state: 'superseded',
    })
    expect(state.selectedModel).toMatchObject({ model_id: previous.model_id, active_flag: false, lifecycle_state: 'superseded' })
  })

  it('merges rollback result and demotes the selected outgoing model', async () => {
    const outgoing = makeBasinsModel({ model_id: 'model-a', active_flag: true, lifecycle_state: 'active' })
    const restoredBefore = makeBasinsModel({ model_id: 'model-b', active_flag: false, lifecycle_state: 'superseded' })
    const restored = makeBasinsModel({ ...restoredBefore, active_flag: true, lifecycle_state: 'active' })
    const demoted = makeBasinsModel({ ...outgoing, active_flag: false, lifecycle_state: 'superseded' })
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'rollback_version',
      status: 'ready',
      model_id: outgoing.model_id,
      basin_version_id: outgoing.basin_version_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'rollback',
      operation: 'rollback_version',
      model: restored,
      previous_model: demoted,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: outgoing.model_id, log_id: 10 },
    }

    useModelAssetsStore.setState({
      models: [outgoing, restoredBefore],
      selectedModel: outgoing,
    })
    vi.mocked(client.POST).mockResolvedValue(success(result) as never)

    await useModelAssetsStore.getState().runModelOperation(outgoing.model_id, {
      operation: 'rollback_version',
      previous_model_id: restoredBefore.model_id,
    })

    const state = useModelAssetsStore.getState()
    expect(state.models.filter((model) => model.active_flag)).toHaveLength(1)
    expect(state.models.find((model) => model.model_id === restoredBefore.model_id)).toMatchObject({
      active_flag: true,
      lifecycle_state: 'active',
    })
    expect(state.models.find((model) => model.model_id === outgoing.model_id)).toMatchObject({
      active_flag: false,
      lifecycle_state: 'superseded',
    })
    expect(state.selectedModel).toMatchObject({ model_id: outgoing.model_id, active_flag: false, lifecycle_state: 'superseded' })
  })

  it('keeps backend forbidden lifecycle errors generic and non-successful', async () => {
    vi.mocked(client.POST).mockResolvedValue(
      { data: undefined, error: { error: { code: 'RBAC_FORBIDDEN', message: 'Actor roles are not authorized.' } } } as never,
    )

    await expect(
      useModelAssetsStore.getState().runModelOperation(BASINS_MODEL_ID, { operation: 'activate' }),
    ).rejects.toThrow('模型操作执行失败')

    expect(useModelAssetsStore.getState()).toMatchObject({
      operationLoading: false,
      operationError: '模型操作执行失败',
      operationResult: null,
    })
  })
})
