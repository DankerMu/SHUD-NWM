export const FORECAST_CHART_POINT_BUDGET = 10_000

export interface ForecastPointBudgetStatus {
  pointBudget: number
  sourcePointCount: number
  retainedPointCount: number
  overBudget: boolean
}

export function createForecastPointBudgetGuard(pointBudget = FORECAST_CHART_POINT_BUDGET) {
  let sourcePointCount = 0
  let retainedPointCount = 0

  return {
    take<T>(points: readonly T[] | null | undefined): T[] {
      const source = Array.isArray(points) ? points : []
      sourcePointCount += source.length
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
        overBudget: sourcePointCount > pointBudget,
      }
    },
  }
}

export function forecastPointBudgetMessage(status: ForecastPointBudgetStatus) {
  return `预报序列超出客户端渲染预算（${status.sourcePointCount}/${status.pointBudget} points），已进入降级显示。`
}

export function isForecastPointOverBudget(status: ForecastPointBudgetStatus | null | undefined) {
  return Boolean(status?.overBudget)
}
