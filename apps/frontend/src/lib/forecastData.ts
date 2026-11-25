export interface ForecastSeriesPoint {
  time: string | number
  value: number
}

export interface ForecastSeries {
  scenario: string
  source?: string
  label: string
  color: '#34d399' | '#22d3ee'
  cycleTime?: string | null
  availableLeadHours?: number | null
  points: ForecastSeriesPoint[]
}

export interface ForecastData {
  segmentId: string
  basinVersionId?: string
  riverNetworkVersionId?: string
  cycle?: string | null
  issueTime: string | null
  unit: string
  series: ForecastSeries[]
  sourceAttribution: string
  cycleAttribution: string
}
