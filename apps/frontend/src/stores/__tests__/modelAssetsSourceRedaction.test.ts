import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import {
  buildModelAssetDependencyGraph,
  buildModelAssetProducts,
  displaySanitizedSource,
  MODEL_ASSET_RESTRICTED_SOURCE,
  sanitizeModelAssetString,
  type ModelAssetPage,
  useModelAssetsStore,
} from '@/stores/modelAssets'

import { BASINS_MODEL_ID, makeBasinsModel, MANIFEST_URI, PACKAGE_URI, resetStore, success } from './modelAssetsFixtures'

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
})
