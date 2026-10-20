import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import {
  buildModelAssetDependencyGraph,
  buildModelAssetMapProjection,
  MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
  MODEL_ASSET_RESTRICTED_SOURCE,
  useModelAssetsStore,
} from '@/stores/modelAssets'

import { BASINS_MODEL_ID, makeBasinsModel, resetStore, success } from './modelAssetsFixtures'

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
})
