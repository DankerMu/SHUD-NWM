interface StationDisplayInput {
  stationId: string
  stationName?: string | null
  basinId?: string | null
}

interface RiverSegmentDisplayInput {
  riverSegmentId: string
  segmentName?: string | null
  basinId?: string | null
}

export interface HydroMetFeatureDisplayName {
  title: string
  meta: string | null
}

export function formatStationDisplayName(input: StationDisplayInput): HydroMetFeatureDisplayName {
  const stationId = normalizedText(input.stationId)
  const stationName = normalizedText(input.stationName)
  const parsed = stationId ? stationTitleFromId(stationId, input.basinId) : null
  const title = stationName && !isGenericStationName(stationName, stationId) ? stationName : parsed ?? stationName ?? stationId ?? '气象代站'
  return { title, meta: stationId ? `站点 ID ${stationId}` : null }
}

export function formatRiverSegmentDisplayName(input: RiverSegmentDisplayInput): HydroMetFeatureDisplayName {
  const riverSegmentId = normalizedText(input.riverSegmentId)
  const segmentName = normalizedText(input.segmentName)
  const parsed = riverSegmentTitleFromId(riverSegmentId, input.basinId)
  const title = segmentName && !isGenericRiverSegmentName(segmentName, riverSegmentId) ? segmentName : parsed ?? segmentName ?? riverSegmentId ?? '河段'
  return { title, meta: riverSegmentId ? `河段 ID ${riverSegmentId}` : null }
}

function stationTitleFromId(stationId: string, basinId?: string | null): string | null {
  const stationMatch = stationId.match(/^(.+?)_forc(?:_[a-z]+)?_(\d+)$/i)
  if (!stationMatch) return null
  const basinCode = basinCodeFromIdentity(basinId) ?? basinCodeFromIdentity(stationMatch[1])
  if (!basinCode) return null
  return `${formatBasinLabel(basinCode)} 代站 ${formatNumericSuffix(stationMatch[2])}`
}

function riverSegmentTitleFromId(riverSegmentId: string | null, basinId?: string | null): string | null {
  if (!riverSegmentId) return null
  const riverMatch = riverSegmentId.match(/_riv_(\d+)$/i)
  if (!riverMatch) return null
  const basinCode = basinCodeFromIdentity(basinId) ?? basinCodeFromRiverSegmentId(riverSegmentId)
  if (!basinCode) return null
  return `${formatBasinLabel(basinCode)} 河段 ${formatNumericSuffix(riverMatch[1])}`
}

function basinCodeFromRiverSegmentId(riverSegmentId: string): string | null {
  const prefix = riverSegmentId.replace(/_riv_\d+$/i, '')
  return basinCodeFromIdentity(prefix)
}

function basinCodeFromIdentity(value?: string | null): string | null {
  const text = normalizedText(value)
  if (!text) return null
  let code = text.toLowerCase()
  code = code.replace(/^basins_/, '')
  code = code.replace(/_vbasins$/, '')
  code = code.replace(/(?:_shud)+$/, '')
  code = code.replace(/_rivnet.*$/, '')
  code = code.replace(/[^a-z0-9_ -]/g, '')
  code = code.replace(/^_+|_+$/g, '')
  return code || null
}

function formatBasinLabel(code: string): string {
  return code
    .split(/[_ -]+/)
    .filter(Boolean)
    .map((token) => (token.length <= 3 ? token.toUpperCase() : `${token[0].toUpperCase()}${token.slice(1)}`))
    .join(' ')
}

function formatNumericSuffix(value: string): string {
  const trimmed = value.replace(/^0+(?=\d)/, '')
  return trimmed || '0'
}

function isGenericStationName(name: string, stationId: string | null): boolean {
  const normalizedName = normalizeComparableName(name)
  if (stationId && normalizedName === normalizeComparableName(stationId)) return true
  return /\bforcing(?:\s+station)?\s*0*\d+\b/.test(normalizedName)
}

function isGenericRiverSegmentName(name: string, riverSegmentId: string | null): boolean {
  const normalizedName = normalizeComparableName(name)
  if (riverSegmentId && normalizedName === normalizeComparableName(riverSegmentId)) return true
  return /\b(?:river\s+)?segment\s*0*\d+\b/.test(normalizedName)
}

function normalizeComparableName(value: string): string {
  return value.trim().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ')
}

function normalizedText(value?: string | null): string | null {
  if (typeof value !== 'string') return null
  const text = value.trim()
  return text.length > 0 ? text : null
}
