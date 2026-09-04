import { RIVER_CLICK_ACCEPTED_SAMPLES } from './constants'

export type RiverClickDurationValidation = { ok: true } | { ok: false; reason: string }

/**
 * Exactly 20 finite non-negative durations. Browser performance.now() returns
 * fractional milliseconds, so any finite non-negative number is accepted.
 */
export function validateRiverClickDurations(durations: number[]): RiverClickDurationValidation {
  if (durations.length !== RIVER_CLICK_ACCEPTED_SAMPLES) {
    return { ok: false, reason: `expected exactly ${RIVER_CLICK_ACCEPTED_SAMPLES} accepted samples, got ${durations.length}` }
  }
  for (const duration of durations) {
    if (typeof duration !== 'number' || !Number.isFinite(duration) || duration < 0) {
      return { ok: false, reason: `sample duration must be a finite non-negative number, got ${String(duration)}` }
    }
  }
  return { ok: true }
}

/**
 * Nearest-rank P95 over exactly 20 sorted ascending values:
 * index ceil(0.95 * N) - 1, i.e. index 18 for N=20. No interpolation.
 * Returns null for any sample count other than 20 or any non-finite/negative
 * duration (never sorts NaN into a wrong result).
 */
export function nearestRankP95(durations: number[]): number | null {
  if (durations.length !== RIVER_CLICK_ACCEPTED_SAMPLES) return null
  for (const duration of durations) {
    if (typeof duration !== 'number' || !Number.isFinite(duration) || duration < 0) return null
  }
  const sorted = [...durations].sort((a, b) => a - b)
  const rank = Math.ceil(0.95 * durations.length) - 1
  return sorted[rank] ?? null
}
