import type { ForecastPointBudgetStatus } from '@/lib/forecastRenderingBudget'
import type { M11Source } from '@/lib/m11/queryState'

export interface ForecastSeriesPoint {
  time: string | number
  value: number
}

export interface ForecastSeries {
  scenario: string
  source?: string
  role?: string
  isAnalysis: boolean
  label: string
  color: '#2266cc' | '#ef7d22' | '#2ca02c' | '#34d399' | '#22d3ee'
  cycleTime?: string | null
  availableLeadHours?: number | null
  points: ForecastSeriesPoint[]
}

export interface ForecastData {
  segmentId: string
  basinVersionId?: string
  riverNetworkVersionId?: string
  source?: M11Source | null
  cycle?: string | null
  issueTime: string | null
  unit: string
  series: ForecastSeries[]
  sourceAttribution: string
  cycleAttribution: string
  pointBudgetStatus?: ForecastPointBudgetStatus
}
