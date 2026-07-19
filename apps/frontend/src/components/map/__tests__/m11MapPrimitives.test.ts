import { describe, expect, it } from 'vitest'

import { m11StationClusterPolicy } from '@/components/map/m11MapPrimitives'

describe('m11StationClusterPolicy', () => {
  it('renders small direct-grid station sets without clustering', () => {
    expect(m11StationClusterPolicy(0)).toEqual({ enabled: false, radius: 0, maxZoom: 0 })
    expect(m11StationClusterPolicy(24)).toEqual({ enabled: false, radius: 0, maxZoom: 0 })
  })

  it('uses bounded regional clustering for medium direct-grid station sets', () => {
    expect(m11StationClusterPolicy(25)).toEqual({ enabled: true, radius: 28, maxZoom: 7 })
    expect(m11StationClusterPolicy(500)).toEqual({ enabled: true, radius: 28, maxZoom: 7 })
  })

  it('keeps dense station sets clustered only through regional zoom', () => {
    expect(m11StationClusterPolicy(501)).toEqual({ enabled: true, radius: 36, maxZoom: 8 })
    expect(m11StationClusterPolicy(3_385)).toEqual({ enabled: true, radius: 36, maxZoom: 8 })
  })
})
