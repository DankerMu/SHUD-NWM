/**
 * Shared 20-duration fixture whose sorted nearest-rank index 18 is exactly 2000.
 * Independent arithmetic: 18 values of 100, then 2000, then 2100.
 * sorted = [100×18, 2000, 2100]; ceil(0.95*20)-1 = 18 → 2000.
 */
export const RIVER_CLICK_EXACT_THRESHOLD_DURATIONS: readonly number[] = [
  2100,
  2000,
  100, 100, 100, 100, 100, 100, 100, 100,
  100, 100, 100, 100, 100, 100, 100, 100,
  100, 100,
]

/** Just-below: sorted[18] = 1999.999, eligible PASS. */
export const RIVER_CLICK_JUST_BELOW_THRESHOLD_DURATIONS: readonly number[] = [
  2100,
  1999.999,
  100, 100, 100, 100, 100, 100, 100, 100,
  100, 100, 100, 100, 100, 100, 100, 100,
  100, 100,
]

/** Strictly above: sorted[18] = 2000.001, FAIL. */
export const RIVER_CLICK_ABOVE_THRESHOLD_DURATIONS: readonly number[] = [
  2100,
  2000.001,
  100, 100, 100, 100, 100, 100, 100, 100,
  100, 100, 100, 100, 100, 100, 100, 100,
  100, 100,
]

export function riverClickExactThresholdSamples(durations: readonly number[] = RIVER_CLICK_EXACT_THRESHOLD_DURATIONS) {
  return durations.map((durationMs, index) => ({
    index: index + 1,
    durationMs,
    gfsStatus: 200,
    ifsStatus: 200,
  }))
}
