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
const MODEL_ASSET_DETAIL_IDENTITY_MISMATCH = '模型资产详情与当前选择不匹配'
const MODEL_ASSET_SANITIZE_MAX_DEPTH = 24
const MODEL_ASSET_SANITIZE_MAX_NODES = 5000
const MODEL_ASSET_GEOMETRY_MAX_DEPTH = 48
const MODEL_ASSET_GEOMETRY_MAX_NODES = 10000

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
  depth?: number
  nodeBudget?: { count: number }
  seen?: WeakSet<object>
}

export function sanitizeModelAssetValue(value: unknown, options: SanitizeOptions = {}): unknown {
  const path = options.path ?? []
  const depth = options.depth ?? 0
  const nodeBudget = options.nodeBudget ?? { count: 0 }
  const restrictedFields = options.restrictedFields

  nodeBudget.count += 1
  if (depth > MODEL_ASSET_SANITIZE_MAX_DEPTH || nodeBudget.count > MODEL_ASSET_SANITIZE_MAX_NODES) {
    if (path.length > 0) restrictedFields?.add(path.join('.'))
    return null
  }

  if (typeof value === 'string') {
    const sanitized = sanitizeModelAssetString(value)
    if (sanitized === null && value.trim() !== '' && path.length > 0) restrictedFields?.add(path.join('.'))
    return sanitized
  }
  if (Array.isArray(value)) {
    const seen = options.seen ?? new WeakSet<object>()
    if (seen.has(value)) {
      if (path.length > 0) restrictedFields?.add(path.join('.'))
      return null
    }
    seen.add(value)
    const sanitized: unknown[] = []
    for (let index = 0; index < value.length; index += 1) {
      if (nodeBudget.count >= MODEL_ASSET_SANITIZE_MAX_NODES) {
        if (path.length > 0) restrictedFields?.add(path.join('.'))
        break
      }
      sanitized.push(
        sanitizeModelAssetValue(value[index], {
          ...options,
          depth: depth + 1,
          nodeBudget,
          path: [...path, String(index)],
          seen,
        }),
      )
    }
    return sanitized
  }
  if (!isJsonRecord(value)) return value

  const seen = options.seen ?? new WeakSet<object>()
  if (seen.has(value)) {
    if (path.length > 0) restrictedFields?.add(path.join('.'))
    return null
  }
  seen.add(value)

  const sanitized: JsonRecord = {}
  for (const [key, entry] of Object.entries(value)) {
    if (nodeBudget.count >= MODEL_ASSET_SANITIZE_MAX_NODES) {
      if (path.length > 0) restrictedFields?.add(path.join('.'))
      break
    }
    sanitized[key] = sanitizeModelAssetValue(entry, {
      ...options,
      depth: depth + 1,
      nodeBudget,
      path: [...path, key],
      seen,
    })
  }
  return sanitized
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
  const restrictedFields = model?.__restrictedSourceFields
  if (!restrictedFields) return false
  return Object.keys(restrictedFields).some(
    (restrictedPath) =>
      restrictedPath === fieldPath ||
      restrictedPath.startsWith(`${fieldPath}.`) ||
      fieldPath.startsWith(`${restrictedPath}.`),
  )
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

function firstTextInArray(value: unknown): string | null {
  if (!Array.isArray(value)) return null
  for (const entry of value) {
    const text = textValue(entry)
    if (text) return text
  }
  return null
}

function displayModelAssetSourceValue(
  model: ModelAsset | null | undefined,
  value: unknown,
  restrictedFieldPaths: string[],
): string {
  if (hasAnyRestrictedModelAssetSource(model, restrictedFieldPaths)) return MODEL_ASSET_RESTRICTED_SOURCE
  const rawValue = textValue(value)
  if (rawValue) return displaySanitizedSource(rawValue)
  return MODEL_ASSET_UNAVAILABLE
}

function projectProductEntry(model: ModelAsset, product: JsonRecord, path: string, index: number): ModelAssetProductAsset {
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
}

function buildExplicitProductProjection(model: ModelAsset, profile: JsonRecord): ModelAssetProductProjection | null {
  const items: ModelAssetProductAsset[] = []
  let truncated = false
  let displayIndex = 0

  productKeys: for (const key of ['product_assets', 'products', 'assets'] as const) {
    const products = profile[key]
    if (!Array.isArray(products)) continue

    for (let index = 0; index < products.length; index += 1) {
      if (items.length >= MODEL_ASSET_PRODUCT_DISPLAY_LIMIT) {
        truncated = true
        break productKeys
      }

      const product = products[index]
      if (!isJsonRecord(product)) continue
      items.push(projectProductEntry(model, product, `resource_profile.${key}.${index}`, displayIndex))
      displayIndex += 1
    }
  }

  if (items.length === 0) return null
  return { items, truncated, notice: truncated ? MODEL_ASSET_PRODUCT_LIMIT_TEXT : null }
}

interface GeometryInspection {
  valid: boolean
  overBudget: boolean
  featureCount: number
  vertexCount: number
}

function inspectGeometryCandidate(value: unknown): GeometryInspection {
  const state = {
    valid: false,
    invalid: false,
    overBudget: false,
    featureCount: 0,
    vertexCount: 0,
    nodes: 0,
  }

  function tick(depth: number) {
    state.nodes += 1
    if (depth > MODEL_ASSET_GEOMETRY_MAX_DEPTH || state.nodes > MODEL_ASSET_GEOMETRY_MAX_NODES) {
      state.overBudget = true
      return false
    }
    return !state.overBudget
  }

  function addFeature() {
    state.featureCount += 1
    state.valid = true
    if (state.featureCount > MODEL_ASSET_MAP_FEATURE_LIMIT) state.overBudget = true
  }

  function addVertex() {
    state.vertexCount += 1
    state.valid = true
    if (state.vertexCount > MODEL_ASSET_MAP_VERTEX_LIMIT) state.overBudget = true
  }

  function countCoordinateVertices(coordinates: unknown, depth: number): boolean {
    if (!tick(depth)) return false
    if (!Array.isArray(coordinates) || coordinates.length === 0) {
      state.invalid = true
      return false
    }
    if (coordinates.length >= 2 && typeof coordinates[0] === 'number' && typeof coordinates[1] === 'number') {
      addVertex()
      return !state.overBudget
    }
    let found = false
    for (const entry of coordinates) {
      const before = state.vertexCount
      if (!countCoordinateVertices(entry, depth + 1)) return false
      found = found || state.vertexCount > before
      if (state.overBudget) return false
    }
    if (!found) state.invalid = true
    return found
  }

  function inspectGeometry(valueToInspect: unknown, depth: number, countAsFeature: boolean): boolean {
    if (!tick(depth)) return false

    if (Array.isArray(valueToInspect)) {
      if (valueToInspect.length === 0) {
        state.invalid = true
        return false
      }
      const first = valueToInspect[0]
      if (isJsonRecord(first) && typeof first.type === 'string') {
        for (const entry of valueToInspect) {
          if (!inspectGeometry(entry, depth + 1, true)) return false
          if (state.overBudget) return false
        }
        return true
      }
      if (countAsFeature) addFeature()
      return countCoordinateVertices(valueToInspect, depth + 1)
    }

    if (!isJsonRecord(valueToInspect) || typeof valueToInspect.type !== 'string') {
      state.invalid = true
      return false
    }

    if (valueToInspect.type === 'FeatureCollection') {
      if (!Array.isArray(valueToInspect.features) || valueToInspect.features.length === 0) {
        state.invalid = true
        return false
      }
      for (const feature of valueToInspect.features) {
        if (!inspectGeometry(feature, depth + 1, true)) return false
        if (state.overBudget) return false
      }
      return true
    }

    if (valueToInspect.type === 'Feature') {
      if (!isJsonRecord(valueToInspect.geometry)) {
        state.invalid = true
        return false
      }
      addFeature()
      return inspectGeometry(valueToInspect.geometry, depth + 1, false)
    }

    if (valueToInspect.type === 'GeometryCollection') {
      if (!Array.isArray(valueToInspect.geometries) || valueToInspect.geometries.length === 0) {
        state.invalid = true
        return false
      }
      for (const geometry of valueToInspect.geometries) {
        if (!inspectGeometry(geometry, depth + 1, true)) return false
        if (state.overBudget) return false
      }
      return true
    }

    if (!Array.isArray(valueToInspect.coordinates)) {
      state.invalid = true
      return false
    }
    if (countAsFeature) addFeature()
    return countCoordinateVertices(valueToInspect.coordinates, depth + 1)
  }

  inspectGeometry(value, 0, true)
  return {
    valid: state.valid && !state.invalid,
    overBudget: state.overBudget,
    featureCount: state.featureCount,
    vertexCount: state.vertexCount,
  }
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
  const rawSource =
    textValue(lineage.source_uri) ??
    textValue(lineage.source_path) ??
    textValue(lineage.local_path) ??
    firstTextInArray(lineage.uris) ??
    textValue(profile.source_path) ??
    textValue(model?.source_uri) ??
    textValue(model?.source_path)
  const sourceValue = displayModelAssetSourceValue(model, rawSource, [
    'resource_profile.source_lineage',
    'resource_profile.source_path',
    'source_uri',
    'source_path',
  ])
  const packageValue = hasRestrictedModelAssetSource(model, 'model_package_uri')
    ? MODEL_ASSET_RESTRICTED_SOURCE
    : displayValue(model?.package_checksum ?? model?.model_package_uri)
  const values: Record<string, { label: string; value: string; missing: boolean }> = {
    model: { label: '模型', value: displayValue(model?.model_id), missing: !textValue(model?.model_id) },
    basin: { label: '流域版本', value: displayValue(model?.basin_version_id), missing: !textValue(model?.basin_version_id) },
    river: { label: '河网版本', value: displayValue(model?.river_network_version_id), missing: !textValue(model?.river_network_version_id) },
    mesh: { label: '网格版本', value: displayValue(model?.mesh_version_id), missing: !textValue(model?.mesh_version_id) },
    calibration: { label: '率定版本', value: displayValue(model?.calibration_version_id), missing: !textValue(model?.calibration_version_id) },
    package: { label: '模型包', value: packageValue, missing: packageValue === MODEL_ASSET_UNAVAILABLE },
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
  const explicitProjection = buildExplicitProductProjection(model, profile)
  const fallbackProducts: ModelAssetProductAsset[] = [
    {
      id: 'model-package',
      label: '模型包',
      checksum: displayValue(model.package_checksum),
      target: displayModelAssetSourceValue(model, model.model_package_uri, ['model_package_uri']),
    },
    {
      id: 'manifest',
      label: 'Manifest',
      checksum: displayValue(model.source_inventory_checksum),
      target: displayModelAssetSourceValue(model, model.manifest_uri, ['manifest_uri']),
    },
    {
      id: 'mesh',
      label: '网格文件',
      checksum: displayValue(model.mesh_checksum),
      target: displayModelAssetSourceValue(model, model.mesh_uri, ['mesh_uri']),
    },
  ].filter((product) => product.target !== MODEL_ASSET_UNAVAILABLE || product.checksum !== MODEL_ASSET_UNAVAILABLE)
  if (explicitProjection) return explicitProjection
  const allProducts = fallbackProducts
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
  const inspected = inspectGeometryCandidate(geometry)
  if (inspected.overBudget) {
    return {
      status: 'over-budget',
      text: MODEL_ASSET_MAP_OVER_BUDGET_TEXT,
      featureCount: inspected.featureCount,
      vertexCount: inspected.vertexCount,
      geometry: null,
    }
  }
  if (!inspected.valid || inspected.featureCount === 0 || inspected.vertexCount === 0) {
    return {
      status: 'missing',
      text: MODEL_ASSET_MAP_UNAVAILABLE_TEXT,
      featureCount: inspected.featureCount,
      vertexCount: inspected.vertexCount,
      geometry: null,
    }
  }
  return {
    status: 'available',
    text: `${inspected.featureCount} 个要素 / ${inspected.vertexCount} 个坐标点`,
    featureCount: inspected.featureCount,
    vertexCount: inspected.vertexCount,
    geometry,
  }
}

export const useModelAssetsStore = create<ModelAssetsState>((set) => {
  let listRequestSeq = 0
  let detailRequestSeq = 0

  return {
  models: [],
  selectedModel: null,
  total: 0,
  limit: 50,
  offset: 0,
  loading: false,
  detailLoading: false,
  error: null,
  fetchModels: async (filters) => {
    const requestSeq = (listRequestSeq += 1)
    detailRequestSeq += 1
    set({ loading: true, selectedModel: null, detailLoading: false, error: null })

    try {
      const page = await getModelPage(filters)
      if (requestSeq !== listRequestSeq) return
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
      if (requestSeq === listRequestSeq) set({ selectedModel: null, detailLoading: false, loading: false, error: message })
      throw error
    }
  },
  fetchModelDetail: async (modelId) => {
    const requestSeq = (detailRequestSeq += 1)
    set({ selectedModel: null, detailLoading: true, error: null })

    try {
      const model = await getModelDetail(modelId)
      if (requestSeq !== detailRequestSeq) return
      if (model.model_id !== modelId) {
        throw new Error(MODEL_ASSET_DETAIL_IDENTITY_MISMATCH)
      }
      set({ selectedModel: normalizeModelAsset(model), detailLoading: false, error: null })
    } catch (error) {
      const message = getApiErrorMessage(error, '模型资产详情加载失败')
      if (requestSeq === detailRequestSeq) set({ selectedModel: null, detailLoading: false, error: message })
      throw error
    }
  },
  clearSelectedModel: () => {
    detailRequestSeq += 1
    set({ selectedModel: null, detailLoading: false })
  },
  }
})
