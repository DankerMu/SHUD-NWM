import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'

export type ModelAsset = components['schemas']['ModelInstance']
export type ModelAssetPage = components['schemas']['ModelInstancePage']
export type ModelAssetActiveFilter = 'true' | 'false' | 'all'

export interface ModelAssetListFilters {
  basinVersionId?: string
  active?: ModelAssetActiveFilter
  limit?: number
  offset?: number
}

interface ModelAssetsState {
  models: ModelAsset[]
  selectedModel: ModelAsset | null
  total: number
  limit: number
  offset: number
  loading: boolean
  detailLoading: boolean
  error: string | null
  fetchModels: (filters?: ModelAssetListFilters) => Promise<void>
  fetchModelDetail: (modelId: string) => Promise<void>
}

async function getModelPage(filters: ModelAssetListFilters = {}) {
  const limit = filters.limit ?? 50
  const offset = filters.offset ?? 0
  const { data, error } = await client.GET('/api/v1/models', {
    params: {
      query: {
        basin_version_id: filters.basinVersionId,
        active: filters.active ?? 'all',
        limit,
        offset,
      },
    },
  })

  if (error) throw new Error(getApiErrorMessage(error, '模型资产列表加载失败'))
  return unwrapApiData<ModelAssetPage>(data, '模型资产列表加载失败')
}

async function getModelDetail(modelId: string) {
  const { data, error } = await client.GET('/api/v1/models/{model_id}', {
    params: { path: { model_id: modelId } },
  })

  if (error) throw new Error(getApiErrorMessage(error, '模型资产详情加载失败'))
  return unwrapApiData<ModelAsset>(data, '模型资产详情加载失败')
}

export const useModelAssetsStore = create<ModelAssetsState>((set) => ({
  models: [],
  selectedModel: null,
  total: 0,
  limit: 50,
  offset: 0,
  loading: false,
  detailLoading: false,
  error: null,
  fetchModels: async (filters) => {
    set({ loading: true, error: null })

    try {
      const page = await getModelPage(filters)
      set({
        models: page.items,
        total: page.total,
        limit: page.limit,
        offset: page.offset,
        loading: false,
        error: null,
      })
    } catch (error) {
      const message = getApiErrorMessage(error, '模型资产列表加载失败')
      set({ loading: false, error: message })
      throw error
    }
  },
  fetchModelDetail: async (modelId) => {
    set({ detailLoading: true, error: null })

    try {
      const model = await getModelDetail(modelId)
      set({ selectedModel: model, detailLoading: false, error: null })
    } catch (error) {
      const message = getApiErrorMessage(error, '模型资产详情加载失败')
      set({ detailLoading: false, error: message })
      throw error
    }
  },
}))
