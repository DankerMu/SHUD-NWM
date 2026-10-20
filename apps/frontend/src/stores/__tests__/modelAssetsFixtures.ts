import { expect } from 'vitest'

import { type ModelAsset, useModelAssetsStore } from '@/stores/modelAssets'

export function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

export function failure(message: string) {
  return { data: undefined, error: { error: { message } } }
}

export const UNSAFE_MODEL_ASSET_ERROR =
  'failed to inspect /volume/data/nwm/Basins/qhh and C:\\nwm\\Basins\\qhh from file:///volume/data/nwm/Basins/qhh?token=abc#frag via https://user:pass@assets.example.test/pkg?token=abc#frag'
export const UNSAFE_MODEL_ASSET_ERROR_TOKENS = [
  '/volume/data/nwm/Basins/qhh',
  'C:\\nwm\\Basins\\qhh',
  'file://',
  'user:pass',
  'token=abc',
  '#frag',
] as const

export function expectNoUnsafeModelAssetErrorText(value: string | null) {
  expect(value).toBeTruthy()
  for (const token of UNSAFE_MODEL_ASSET_ERROR_TOKENS) {
    expect(value).not.toContain(token)
  }
}

export const BASINS_MODEL_ID = 'basins_basin_a_shud'
export const MANIFEST_URI = 's3://nhms/models/basins_basin_a_shud/vbasins/manifest.json'
export const PACKAGE_URI = 's3://nhms/models/basins_basin_a_shud/vbasins/package/'

export function makeBasinsModel(overrides: Partial<ModelAsset> = {}): ModelAsset {
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
    lifecycle_state: 'inactive',
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

export function resetStore() {
  useModelAssetsStore.setState(useModelAssetsStore.getInitialState(), true)
}
