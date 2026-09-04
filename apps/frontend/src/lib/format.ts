export function formatDate(value: string | number | Date | null | undefined) {
  if (!value) return '-'

  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return '-'

  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || seconds < 0) return '-'

  const totalSeconds = Math.round(seconds)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const remainingSeconds = totalSeconds % 60

  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`
  return `${remainingSeconds}s`
}

/** Exponents the display layer is allowed to lift, keyed by the ASCII digit. */
const UNIT_SUPERSCRIPTS: Record<string, string> = { '2': '²', '3': '³' }

/**
 * Render a unit for humans: `m3/s` becomes `m³/s`, `mm2` becomes `mm²`.
 *
 * DISPLAY ONLY. The stored and transported spelling stays ASCII — `m3/s` is a
 * value of the `hydro.river_unit` Postgres enum and travels verbatim through the
 * API, so nothing that compares, keys, or persists a unit may call this. Use it
 * at the point a unit is put on screen and nowhere else.
 *
 * Only a digit that directly follows a letter is lifted, which is what keeps
 * `m3/s` (exponent) apart from a unit whose digit is part of a name.
 */
export function formatUnitForDisplay(unit: string | null | undefined): string {
  if (!unit) return ''
  return unit.replace(/(?<=[a-zA-Z])([23])/g, (digit) => UNIT_SUPERSCRIPTS[digit])
}
