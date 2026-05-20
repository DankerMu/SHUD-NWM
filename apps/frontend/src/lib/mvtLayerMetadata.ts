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

export function buildMvtTileUrlTemplate(
  metadata: MvtLayerMetadata,
  replacements: Record<string, string>,
): string {
  let template = metadata.url_template
  for (const [key, value] of Object.entries(replacements)) {
    template = template.replaceAll(`{${key}}`, encodeURIComponent(value))
  }
  return buildApiUrl(template)
    .replaceAll('%7Bz%7D', '{z}')
    .replaceAll('%7Bx%7D', '{x}')
    .replaceAll('%7By%7D', '{y}')
}

export async function fetchLayerCatalogMetadata(signal?: AbortSignal): Promise<components['schemas']['Layer'][]> {
  const response = await apiFetch('/api/v1/layers?limit=100&offset=0', { signal })
  if (!response.ok) return []
  const body = (await response.json()) as unknown
  if (!isRecord(body) || !Array.isArray(body.data)) return []
  return body.data.filter(isLayerRecord)
}

function isLayerRecord(value: unknown): value is components['schemas']['Layer'] {
  return isRecord(value) && typeof value.layer_id === 'string' && typeof value.layer_name === 'string'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
