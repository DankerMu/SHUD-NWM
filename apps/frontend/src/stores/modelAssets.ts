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

type JsonRecord = Record<string, unknown>

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

function isJsonRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isPublicUriLike(value: string) {
  return /^(?!file:)[a-z][a-z0-9+.-]*:\/\//i.test(value) || value.startsWith('//')
}

function redactLocalSourcePaths(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactLocalSourcePaths)
  if (!isJsonRecord(value)) return value

  return Object.fromEntries(
    Object.entries(value).map(([key, entry]) => {
      if ((key === 'source_path' || key === 'resolved_source_path') && typeof entry === 'string') {
        return [key, isPublicUriLike(entry) ? entry : null]
      }
      return [key, redactLocalSourcePaths(entry)]
    }),
  )
}

function normalizeModelAsset(model: ModelAsset): ModelAsset {
  return redactLocalSourcePaths(model) as ModelAsset
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
        models: page.items.map(normalizeModelAsset),
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
    set({ selectedModel: null, detailLoading: true, error: null })

    try {
      const model = await getModelDetail(modelId)
      set({ selectedModel: normalizeModelAsset(model), detailLoading: false, error: null })
    } catch (error) {
      const message = getApiErrorMessage(error, '模型资产详情加载失败')
      set({ selectedModel: null, detailLoading: false, error: message })
      throw error
    }
  },
}))
