import { describe, expect, it } from 'vitest'

import type { components } from '@/api/types'

type ModelInstance = components['schemas']['ModelInstance']

describe('model asset API types', () => {
  it('carry Basins-backed model asset metadata for asset management views', () => {
    const basinsModel: ModelInstance = {
      model_id: 'basins_basin_a_shud',
      model_name: 'alias-a',
      basin_id: 'basins_basin_a',
      basin_name: 'Basin A',
      basin_version_id: 'basins_basin_a_vbasins',
      river_network_version_id: 'basins_basin_a_rivnet_vbasins',
      mesh_version_id: 'basins_basin_a_mesh_vbasins',
      calibration_version_id: 'basins_basin_a_shud_calib_vbasins',
      segment_count: 2,
      mesh_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh',
      mesh_checksum: 'mesh-sha-1',
      shud_code_version: 'basins-shud',
      active_flag: false,
      model_package_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/',
      package_checksum: 'package-sha-1',
      manifest_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/manifest.json',
      source_inventory_checksum: 'inventory-sha-1',
      basin_slug: 'basin-a',
      shud_input_name: 'alias-a',
      resource_profile: { lineage: 'basins_registry_import' },
      created_at: '2026-05-14T00:00:00Z',
    }

    expect(basinsModel.segment_count).toBe(2)
    expect(basinsModel.package_checksum).toBe('package-sha-1')
    expect(basinsModel.source_inventory_checksum).toBe('inventory-sha-1')
  })
})
