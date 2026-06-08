import { apiFetch, buildApiUrl } from '@/api/base'
import type { components } from '@/api/types'

export type MvtLayerMetadata = components['schemas']['LayerMetadata'] & {
  tile_format: 'mvt'
  url_template: string
  maplibre_source_layer: string
}

export interface RunSourceIdentity {
  run_id?: string | null
  basin_version_id?: string | null
  river_network_version_id?: string | null
}

export function isMvtLayerMetadata(value: unknown): value is MvtLayerMetadata {
  if (!isRecord(value)) return false
  return (
    value.tile_format === 'mvt' &&
    typeof value.url_template === 'string' &&
    value.url_template.includes('{z}') &&
    value.url_template.includes('{x}') &&
    value.url_template.includes('{y}') &&
    typeof value.maplibre_source_layer === 'string' &&
    value.maplibre_source_layer.length > 0
  )
}

/**
 * National 总览形态：MVT 元数据但 required_placeholders 不含 run_id（无 {run_id} 占位的多流域并集瓦片）。
 * 用于让 overlay 走"不要求单 run"的注册分支；basin detail 单 run 模板（含 run_id）返回 false。
 */
export function isNationalOverlayMetadata(value: unknown): value is MvtLayerMetadata {
  return (
    isMvtLayerMetadata(value) &&
    Array.isArray(value.required_placeholders) &&
    !value.required_placeholders.includes('run_id')
  )
}

export function buildMvtTileUrlTemplate(metadata: MvtLayerMetadata, replacements: Record<string, string>): string {
  let template = metadata.url_template
  for (const [key, value] of Object.entries(replacements)) {
    template = template.replaceAll(`{${key}}`, encodeURIComponent(value))
  }
  const url = buildApiUrl(template)
    .replaceAll('%7Bz%7D', '{z}')
    .replaceAll('%7Bx%7D', '{x}')
    .replaceAll('%7By%7D', '{y}')
  return appendMvtCacheVersion(url, metadata)
}

export async function fetchLayerCatalogMetadata(
  signal?: AbortSignal,
  runId?: string | null,
): Promise<components['schemas']['Layer'][]> {
  const params = new URLSearchParams({ limit: '100', offset: '0' })
  if (runId) params.set('run_id', runId)
  const response = await apiFetch(`/api/v1/layers?${params.toString()}`, { signal })
  if (!response.ok) return []
  const body = (await response.json()) as unknown
  if (!isRecord(body) || !Array.isArray(body.data)) return []
  return body.data.filter(isLayerRecord)
}

export function metadataMatchesRun(metadata: MvtLayerMetadata, runId: string, identity?: RunSourceIdentity | null): boolean {
  const metadataRunId = metadata.source_refs?.run_id
  if (typeof metadataRunId === 'string' && metadataRunId !== runId) return false
  const metadataBasinVersionId = metadata.source_refs?.basin_version_id
  if (
    identity?.basin_version_id &&
    typeof metadataBasinVersionId === 'string' &&
    metadataBasinVersionId !== identity.basin_version_id
  ) {
    return false
  }
  const metadataRiverNetworkVersionId = metadata.source_refs?.river_network_version_id
  if (
    identity?.river_network_version_id &&
    typeof metadataRiverNetworkVersionId === 'string' &&
    metadataRiverNetworkVersionId !== identity.river_network_version_id
  ) {
    return false
  }
  return true
}

export function metadataHasValidTime(metadata: MvtLayerMetadata, validTime: string | null | undefined): boolean {
  const selected = normalizeMvtValidTime(validTime)
  if (!selected || !Array.isArray(metadata.valid_times)) return false
  return metadata.valid_times.some((metadataValidTime) => normalizeMvtValidTime(metadataValidTime) === selected)
}

export function isRunMismatchMetadata(metadata: unknown, runId: string): boolean {
  return isMvtLayerMetadata(metadata) && !metadataMatchesRun(metadata, runId)
}

function normalizeMvtValidTime(value: string | null | undefined): string | null {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}

function isLayerRecord(value: unknown): value is components['schemas']['Layer'] {
  return isRecord(value) && typeof value.layer_id === 'string' && typeof value.layer_name === 'string'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function appendMvtCacheVersion(url: string, metadata: MvtLayerMetadata): string {
  const token = mvtCacheVersionToken(metadata)
  if (!token) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}_mvt_cache_version=${encodeURIComponent(token)}`
}

function mvtCacheVersionToken(metadata: MvtLayerMetadata): string | null {
  if (metadata.cache_version) return metadata.cache_version
  if (metadata.cache_etag) return metadata.cache_etag
  const basis = {
    encoder_version: metadata.encoder_version ?? null,
    schema_version: metadata.schema_version ?? metadata.property_schema_version ?? null,
    source_refs: stableRecord(metadata.source_refs ?? null),
  }
  if (!basis.encoder_version && !basis.schema_version && !basis.source_refs) return null
  return JSON.stringify(basis)
}

function stableRecord(value: unknown): unknown {
  if (!isRecord(value)) return value ?? null
  return Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right)))
}
