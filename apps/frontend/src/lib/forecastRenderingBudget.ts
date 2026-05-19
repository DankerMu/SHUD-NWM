export const FORECAST_CHART_POINT_BUDGET = 10_000
export const FORECAST_CHART_SERIES_BUDGET = FORECAST_CHART_POINT_BUDGET

export interface ForecastPointBudgetStatus {
  pointBudget: number
  sourcePointCount: number
  retainedPointCount: number
  seriesBudget: number
  sourceSeriesCount: number
  retainedSeriesCount: number
  overBudget: boolean
}

export function createForecastPointBudgetGuard(
  pointBudget = FORECAST_CHART_POINT_BUDGET,
  seriesBudget = FORECAST_CHART_SERIES_BUDGET,
) {
  let sourcePointCount = 0
  let retainedPointCount = 0
  let sourceSeriesCount = 0
  let retainedSeriesCount = 0
  let explicitSourcePointCount = false
  let explicitSourceSeriesCount = false

  return {
    setSourcePointCount(count: number) {
      sourcePointCount = Math.max(0, count)
      explicitSourcePointCount = true
    },
    setSourceSeriesCount(count: number) {
      sourceSeriesCount = Math.max(0, count)
      explicitSourceSeriesCount = true
    },
    takeSeries<T>(points: readonly T[] | null | undefined): T[] | null {
      if (retainedSeriesCount >= seriesBudget || retainedPointCount >= pointBudget) return null
      retainedSeriesCount += 1
      return this.take(points)
    },
    take<T>(points: readonly T[] | null | undefined): T[] {
      const source = Array.isArray(points) ? points : []
      if (!explicitSourceSeriesCount) sourceSeriesCount += 1
      if (!explicitSourcePointCount) sourcePointCount += source.length
      const remaining = Math.max(0, pointBudget - retainedPointCount)
      if (remaining === 0) return []
      const retained = source.slice(0, remaining)
      retainedPointCount += retained.length
      return retained
    },
    status(): ForecastPointBudgetStatus {
      return {
        pointBudget,
        sourcePointCount,
        retainedPointCount,
        seriesBudget,
        sourceSeriesCount,
        retainedSeriesCount,
        overBudget: sourcePointCount > pointBudget || sourceSeriesCount > seriesBudget,
      }
    },
  }
}

export function forecastPointBudgetMessage(status: ForecastPointBudgetStatus) {
  return `预报序列超出客户端渲染预算（${status.sourcePointCount}/${status.pointBudget} points，${status.sourceSeriesCount}/${status.seriesBudget} series），已进入降级显示。`
}

export function isForecastPointOverBudget(status: ForecastPointBudgetStatus | null | undefined) {
  return Boolean(status?.overBudget)
}
