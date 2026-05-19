import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'

export type ModelAsset = components['schemas']['ModelInstance'] & {
  __restrictedSourceFields?: Record<string, true>
}
export type ModelAssetPage = components['schemas']['ModelInstancePage']
export type ModelAssetActiveFilter = 'true' | 'false' | 'all'

export const MODEL_ASSET_PRODUCT_DISPLAY_LIMIT = 12
export const MODEL_ASSET_MAP_FEATURE_LIMIT = 50
export const MODEL_ASSET_MAP_VERTEX_LIMIT = 2000
export const MODEL_ASSET_UNAVAILABLE = '暂不可用'
export const MODEL_ASSET_RESTRICTED_SOURCE = '受限来源'
export const MODEL_ASSET_PRODUCT_LIMIT_TEXT = '仅显示前 12 个资产'
export const MODEL_ASSET_MAP_OVER_BUDGET_TEXT = '空间几何超出预览预算'
export const MODEL_ASSET_MAP_UNAVAILABLE_TEXT = '暂无空间预览'

export interface ModelAssetListFilters {
  basinVersionId?: string
  active?: ModelAssetActiveFilter
  limit?: number
  offset?: number
}

type JsonRecord = Record<string, unknown>

export interface ModelAssetTreeNode {
  basinName: string
  basinId: string | null
  models: ModelAsset[]
}

export interface ModelAssetTreeState {
  groups: ModelAssetTreeNode[]
  selectedInFilter: boolean
  emptyMessage: string | null
}

export interface ModelAssetKpiCard {
  label: string
  value: string
}

export interface ModelAssetGraphNode {
  id: string
  label: string
  value: string
  missing: boolean
}

export interface ModelAssetGraphEdge {
  from: string
  to: string
}

export interface ModelAssetGraph {
  nodes: ModelAssetGraphNode[]
  edges: ModelAssetGraphEdge[]
}

export interface ModelAssetProductAsset {
  id: string
  label: string
  checksum: string
  target: string
}

export interface ModelAssetProductProjection {
  items: ModelAssetProductAsset[]
  truncated: boolean
  notice: string | null
}

export interface ModelAssetMapProjection {
  status: 'available' | 'missing' | 'over-budget'
  text: string
  featureCount: number
  vertexCount: number
  geometry: unknown | null
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
  clearSelectedModel: () => void
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

function isFileUri(value: string) {
  return /^file:\/\//i.test(value)
}

function isWindowsAbsolutePath(value: string) {
  return /^[a-z]:[\\/]/i.test(value) || /^\\\\[^\\]+\\[^\\]+/.test(value)
}

function isUnixAbsolutePath(value: string) {
  return value.startsWith('/')
}

function isLocalOrFileSource(value: string) {
  return isFileUri(value) || isWindowsAbsolutePath(value) || isUnixAbsolutePath(value)
}

function isPublicUriLike(value: string) {
  return /^(?!file:)[a-z][a-z0-9+.-]*:\/\//i.test(value) || value.startsWith('//')
}

export function sanitizeModelAssetString(value: string): string | null {
  if (isLocalOrFileSource(value)) return null
  if (!isPublicUriLike(value)) return value

  try {
    if (value.startsWith('//')) {
      const parsed = new URL(`https:${value}`)
      return `//${parsed.host}${parsed.pathname}`
    }

    const parsed = new URL(value)
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`
  } catch {
    return value.replace(/\/\/[^/@]+@/, '//').replace(/[?#].*$/, '')
  }
}

export function displaySanitizedSource(value: unknown): string {
  if (typeof value !== 'string' || value.trim() === '') return MODEL_ASSET_UNAVAILABLE
  return sanitizeModelAssetString(value) ?? MODEL_ASSET_RESTRICTED_SOURCE
}

interface SanitizeOptions {
  path?: string[]
  restrictedFields?: Set<string>
}

export function sanitizeModelAssetValue(value: unknown, options: SanitizeOptions = {}): unknown {
  if (typeof value === 'string') {
    const sanitized = sanitizeModelAssetString(value)
    if (sanitized === null && value.trim() !== '') options.restrictedFields?.add((options.path ?? []).join('.'))
    return sanitized
  }
  if (Array.isArray(value)) {
    return value.map((entry, index) =>
      sanitizeModelAssetValue(entry, {
        ...options,
        path: [...(options.path ?? []), String(index)],
      }),
    )
  }
  if (!isJsonRecord(value)) return value

  return Object.fromEntries(
    Object.entries(value).map(([key, entry]) => [
      key,
      sanitizeModelAssetValue(entry, {
        ...options,
        path: [...(options.path ?? []), key],
      }),
    ]),
  )
}

function normalizeModelAsset(model: ModelAsset): ModelAsset {
  const restrictedFields = new Set<string>()
  const sanitized = sanitizeModelAssetValue(model, { restrictedFields }) as ModelAsset
  if (restrictedFields.size === 0) return sanitized
  return {
    ...sanitized,
    __restrictedSourceFields: Object.fromEntries(Array.from(restrictedFields).map((field) => [field, true])),
  }
}

export function hasRestrictedModelAssetSource(model: ModelAsset | null | undefined, fieldPath: string): boolean {
  return Boolean(model?.__restrictedSourceFields?.[fieldPath])
}

function hasAnyRestrictedModelAssetSource(model: ModelAsset | null | undefined, fieldPaths: string[]): boolean {
  return fieldPaths.some((fieldPath) => hasRestrictedModelAssetSource(model, fieldPath))
}

function textValue(value: unknown): string | null {
  if (typeof value === 'string') {
    const trimmed = value.trim()
    return trimmed === '' ? null : trimmed
  }
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return null
}

function displayValue(value: unknown): string {
  return textValue(value) ?? MODEL_ASSET_UNAVAILABLE
}

function getResourceProfile(model: ModelAsset | null): JsonRecord {
  return isJsonRecord(model?.resource_profile) ? model.resource_profile : {}
}

function getNestedRecord(record: JsonRecord, key: string): JsonRecord {
  return isJsonRecord(record[key]) ? record[key] : {}
}

function getFirstText(record: JsonRecord, keys: string[]): string | null {
  for (const key of keys) {
    const value = textValue(record[key])
    if (value) return value
  }
  return null
}

function asRecordArray(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter(isJsonRecord) : []
}

function getExplicitProductEntries(profile: JsonRecord): Array<{ product: JsonRecord; path: string }> {
  return (['product_assets', 'products', 'assets'] as const).flatMap((key) =>
    asRecordArray(profile[key]).map((product, index) => ({ product, path: `resource_profile.${key}.${index}` })),
  )
}

function countVertices(value: unknown): number {
  if (!Array.isArray(value)) return 0
  if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number') return 1
  return value.reduce((sum, entry) => sum + countVertices(entry), 0)
}

function featureCountForGeometry(value: unknown): number {
  if (!isJsonRecord(value)) return 0
  if (value.type === 'FeatureCollection' && Array.isArray(value.features)) return value.features.length
  if (value.type === 'Feature') return 1
  if (typeof value.type === 'string' && 'coordinates' in value) return 1
  return 0
}

function vertexCountForGeometry(value: unknown): number {
  if (!isJsonRecord(value)) return 0
  if (value.type === 'FeatureCollection' && Array.isArray(value.features)) {
    return value.features.reduce((sum, feature) => sum + vertexCountForGeometry(feature), 0)
  }
  if (value.type === 'Feature') return vertexCountForGeometry(value.geometry)
  return countVertices(value.coordinates)
}

function candidateGeometry(profile: JsonRecord): unknown | null {
  const keys = [
    'geometry',
    'geom',
    'basin_geometry',
    'basin_boundary',
    'boundary',
    'river_geometry',
    'river_geometries',
  ]
  for (const key of keys) {
    if (profile[key]) return profile[key]
  }
  return null
}

export function buildModelAssetTree(
  models: ModelAsset[],
  options: { search?: string; active?: ModelAssetActiveFilter; selectedModelId?: string | null } = {},
): ModelAssetTreeState {
  const query = options.search?.trim().toLowerCase() ?? ''
  const active = options.active ?? 'all'
  const filtered = models.filter((model) => {
    if (active !== 'all' && String(model.active_flag) !== active) return false
    if (!query) return true
    return [
      model.model_id,
      model.model_name,
      model.basin_id,
      model.basin_name,
      model.basin_version_id,
      model.basin_slug,
      model.shud_input_name,
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query))
  })

  const groupsByKey = new Map<string, ModelAssetTreeNode>()
  filtered.forEach((model) => {
    const basinName = textValue(model.basin_name) ?? textValue(model.basin_id) ?? '未命名流域'
    const basinId = textValue(model.basin_id)
    const key = `${basinName}:${basinId ?? ''}`
    const group = groupsByKey.get(key) ?? { basinName, basinId, models: [] }
    group.models.push(model)
    groupsByKey.set(key, group)
  })

  const groups = Array.from(groupsByKey.values())
    .map((group) => ({
      ...group,
      models: [...group.models].sort((a, b) => a.model_id.localeCompare(b.model_id)),
    }))
    .sort((a, b) => a.basinName.localeCompare(b.basinName))

  const selectedInFilter = options.selectedModelId
    ? filtered.some((model) => model.model_id === options.selectedModelId)
    : false

  const emptyMessage = models.length === 0 ? '暂无模型资产' : filtered.length === 0 ? '无匹配模型' : null

  return { groups, selectedInFilter, emptyMessage }
}

export function buildModelAssetKpis(model: ModelAsset | null): ModelAssetKpiCard[] {
  const profile = getResourceProfile(model)
  const area = textValue(profile.area_km2)
  const segmentCount = textValue(model?.segment_count ?? profile.segment_count)
  const meshParts = [textValue(model?.mesh_version_id), textValue(model?.mesh_checksum)].filter(Boolean)
  const shudParts = [textValue(model?.shud_code_version), textValue(model?.model_id)].filter(Boolean)
  const segmentParts = [
    segmentCount ? `${segmentCount} 河段` : null,
    area ? `${area} km²` : null,
  ].filter(Boolean)

  return [
    { label: '流域版本', value: displayValue(model?.basin_version_id) },
    { label: '河网版本', value: displayValue(model?.river_network_version_id) },
    { label: '网格版本', value: meshParts.length > 0 ? meshParts.join(' / ') : MODEL_ASSET_UNAVAILABLE },
    { label: '率定版本', value: displayValue(model?.calibration_version_id) },
    { label: 'SHUD / 模型', value: shudParts.length > 0 ? shudParts.join(' / ') : MODEL_ASSET_UNAVAILABLE },
    { label: '河段 / 面积', value: segmentParts.length > 0 ? segmentParts.join(' / ') : MODEL_ASSET_UNAVAILABLE },
  ]
}

export function buildModelAssetDependencyGraph(model: ModelAsset | null): ModelAssetGraph {
  const profile = getResourceProfile(model)
  const lineage = getNestedRecord(profile, 'source_lineage')
  const rawSource = textValue(lineage.source_uri) ?? textValue(model?.source_uri) ?? textValue(lineage.source_path) ?? textValue(model?.source_path)
  const sourceValue = rawSource
    ? displaySanitizedSource(rawSource)
    : hasAnyRestrictedModelAssetSource(model, [
        'resource_profile.source_lineage.source_uri',
        'source_uri',
        'resource_profile.source_lineage.source_path',
        'source_path',
      ])
      ? MODEL_ASSET_RESTRICTED_SOURCE
      : MODEL_ASSET_UNAVAILABLE
  const values: Record<string, { label: string; value: string; missing: boolean }> = {
    model: { label: '模型', value: displayValue(model?.model_id), missing: !textValue(model?.model_id) },
    basin: { label: '流域版本', value: displayValue(model?.basin_version_id), missing: !textValue(model?.basin_version_id) },
    river: { label: '河网版本', value: displayValue(model?.river_network_version_id), missing: !textValue(model?.river_network_version_id) },
    mesh: { label: '网格版本', value: displayValue(model?.mesh_version_id), missing: !textValue(model?.mesh_version_id) },
    calibration: { label: '率定版本', value: displayValue(model?.calibration_version_id), missing: !textValue(model?.calibration_version_id) },
    package: { label: '模型包', value: displayValue(model?.package_checksum ?? model?.model_package_uri), missing: !textValue(model?.package_checksum ?? model?.model_package_uri) },
    source: { label: '来源', value: sourceValue, missing: sourceValue === MODEL_ASSET_UNAVAILABLE },
  }
  const nodes = Object.entries(values).map(([id, node]) => ({ id, ...node }))
  const edges: ModelAssetGraphEdge[] = []
  ;(['basin', 'river', 'mesh', 'calibration', 'package'] as const).forEach((id) => {
    if (!values.model.missing && !values[id].missing) edges.push({ from: 'model', to: id })
  })
  if (!values.package.missing && !values.source.missing) edges.push({ from: 'package', to: 'source' })
  return { nodes, edges }
}

export function buildModelAssetProducts(model: ModelAsset | null): ModelAssetProductProjection {
  if (!model) return { items: [], truncated: false, notice: null }
  const profile = getResourceProfile(model)
  const explicitProducts = getExplicitProductEntries(profile).map(({ product, path }, index) => {
    const target = getFirstText(product, ['target', 'uri', 'url', 'href', 'path', 'source_uri'])
    const targetRestricted = ['target', 'uri', 'url', 'href', 'path', 'source_uri'].some((key) =>
      hasRestrictedModelAssetSource(model, `${path}.${key}`),
    )
    const sanitizedTarget = target
      ? displaySanitizedSource(target)
      : targetRestricted
        ? MODEL_ASSET_RESTRICTED_SOURCE
        : MODEL_ASSET_UNAVAILABLE
    const id = getFirstText(product, ['id', 'asset_id', 'key', 'name']) ?? `asset-${index + 1}`
    const label = getFirstText(product, ['label', 'name', 'type', 'kind']) ?? id
    const checksum = getFirstText(product, ['checksum', 'sha256', 'hash']) ?? MODEL_ASSET_UNAVAILABLE
    return {
      id: sanitizeModelAssetString(id) ?? `asset-${index + 1}`,
      label: sanitizeModelAssetString(label) ?? MODEL_ASSET_RESTRICTED_SOURCE,
      checksum: sanitizeModelAssetString(checksum) ?? MODEL_ASSET_RESTRICTED_SOURCE,
      target: sanitizedTarget,
    }
  })
  const fallbackProducts: ModelAssetProductAsset[] = [
    {
      id: 'model-package',
      label: '模型包',
      checksum: displayValue(model.package_checksum),
      target: model.model_package_uri ? displaySanitizedSource(model.model_package_uri) : MODEL_ASSET_UNAVAILABLE,
    },
    {
      id: 'manifest',
      label: 'Manifest',
      checksum: displayValue(model.source_inventory_checksum),
      target: model.manifest_uri ? displaySanitizedSource(model.manifest_uri) : MODEL_ASSET_UNAVAILABLE,
    },
    {
      id: 'mesh',
      label: '网格文件',
      checksum: displayValue(model.mesh_checksum),
      target: model.mesh_uri ? displaySanitizedSource(model.mesh_uri) : MODEL_ASSET_UNAVAILABLE,
    },
  ].filter((product) => product.target !== MODEL_ASSET_UNAVAILABLE || product.checksum !== MODEL_ASSET_UNAVAILABLE)
  const allProducts = explicitProducts.length > 0 ? explicitProducts : fallbackProducts
  const items = allProducts.slice(0, MODEL_ASSET_PRODUCT_DISPLAY_LIMIT)
  const truncated = allProducts.length > MODEL_ASSET_PRODUCT_DISPLAY_LIMIT
  return { items, truncated, notice: truncated ? MODEL_ASSET_PRODUCT_LIMIT_TEXT : null }
}

export function buildModelAssetMapProjection(model: ModelAsset | null): ModelAssetMapProjection {
  const geometry = candidateGeometry(getResourceProfile(model))
  if (!geometry) {
    return {
      status: 'missing',
      text: MODEL_ASSET_MAP_UNAVAILABLE_TEXT,
      featureCount: 0,
      vertexCount: 0,
      geometry: null,
    }
  }
  const featureCount = featureCountForGeometry(geometry)
  const vertexCount = vertexCountForGeometry(geometry)
  if (featureCount > MODEL_ASSET_MAP_FEATURE_LIMIT || vertexCount > MODEL_ASSET_MAP_VERTEX_LIMIT) {
    return {
      status: 'over-budget',
      text: MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
      featureCount,
      vertexCount,
      geometry: null,
    }
  }
  return {
    status: 'available',
    text: `${featureCount} 个要素 / ${vertexCount} 个坐标点`,
    featureCount,
    vertexCount,
    geometry,
  }
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
      set((state) => ({
        models: page.items.map(normalizeModelAsset),
        total: page.total,
        limit: page.limit,
        offset: page.offset,
        loading: false,
        error: null,
        selectedModel:
          state.selectedModel &&
          page.items.some((model) => model.model_id === state.selectedModel?.model_id)
            ? state.selectedModel
            : null,
      }))
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
  clearSelectedModel: () => set({ selectedModel: null, detailLoading: false }),
}))
