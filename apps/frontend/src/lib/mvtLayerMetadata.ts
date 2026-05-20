import { apiFetch, buildApiUrl } from '@/api/base'
import type { components } from '@/api/types'

export type MvtLayerMetadata = components['schemas']['LayerMetadata'] & {
  tile_format: 'mvt'
  url_template: string
  maplibre_source_layer: string
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

export function metadataMatchesRun(metadata: MvtLayerMetadata, runId: string): boolean {
  const metadataRunId = metadata.source_refs?.run_id
  return typeof metadataRunId !== 'string' || metadataRunId === runId
}

export function isRunMismatchMetadata(metadata: unknown, runId: string): boolean {
  return isMvtLayerMetadata(metadata) && !metadataMatchesRun(metadata, runId)
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
