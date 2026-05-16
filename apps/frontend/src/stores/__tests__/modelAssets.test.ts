import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import type { ModelAsset, ModelAssetPage } from '@/stores/modelAssets'
import { useModelAssetsStore } from '@/stores/modelAssets'

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
      source_is_symlink: false,
    })
    expect(state.models[0].resource_profile).toMatchObject({
      package_checksum: 'package-sha-1',
      source_inventory_checksum: 'inventory-sha-1',
      mesh: { checksum: 'mesh-sha-1' },
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
      source_path: '/volume/data/nwm/Basins/basin-a',
      resolved_source_path: '/volume/data/nwm/Basins/basin-a',
      source_uri: 's3://nhms/sources/basin-a',
    })
    expect(selected?.resource_profile).toMatchObject({
      active_source: 'operator_activation',
      segment_count: 108,
      source_lineage: {
        source_path: '/volume/data/nwm/Basins/basin-a',
        source_uri: 's3://nhms/sources/basin-a',
      },
    })
    expect(useModelAssetsStore.getState().detailLoading).toBe(false)
    expect(useModelAssetsStore.getState().error).toBeNull()
  })

  it('keeps API errors in state for asset-management callers', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure('model registry unavailable') as never)

    await expect(useModelAssetsStore.getState().fetchModels()).rejects.toThrow('model registry unavailable')

    expect(useModelAssetsStore.getState()).toMatchObject({
      loading: false,
      error: 'model registry unavailable',
    })
  })
})
