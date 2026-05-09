import { create } from 'zustand'

export interface ForecastPoint {
  time: string
  value: number
  source?: 'analysis' | 'forecast'
}

export interface ForecastState {
  selectedSegment: string | null
  forecastData: ForecastPoint[]
  loading: boolean
  error: string | null
  setSelectedSegment: (segmentId: string | null) => void
  setForecastData: (data: ForecastPoint[]) => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void
}

export const useForecastStore = create<ForecastState>((set) => ({
  selectedSegment: null,
  forecastData: [],
  loading: false,
  error: null,
  setSelectedSegment: (selectedSegment) => set({ selectedSegment }),
  setForecastData: (forecastData) => set({ forecastData }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}))
