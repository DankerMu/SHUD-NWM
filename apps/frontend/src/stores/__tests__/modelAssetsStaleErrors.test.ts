import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { buildModelAssetTree, type ModelAsset, useModelAssetsStore } from '@/stores/modelAssets'

import {
  BASINS_MODEL_ID,
  expectNoUnsafeModelAssetErrorText,
  failure,
  makeBasinsModel,
  resetStore,
  success,
  UNSAFE_MODEL_ASSET_ERROR,
} from './modelAssetsFixtures'

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
})
