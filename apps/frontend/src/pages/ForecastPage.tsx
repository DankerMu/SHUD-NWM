import { useCallback } from 'react'

import { ForecastPanel } from '@/components/forecast/ForecastPanel'
import { MapView } from '@/components/map/MapView'
import { useToast } from '@/hooks/useToast'
import { getApiErrorMessage } from '@/api/response'
import { cn } from '@/lib/cn'
import { useForecastStore, type ForecastSegmentInfo } from '@/stores/forecast'

export function ForecastPage() {
  const selectedSegment = useForecastStore((state) => state.selectedSegment)
  const forecastData = useForecastStore((state) => state.forecastData)
  const loading = useForecastStore((state) => state.loading)
  const error = useForecastStore((state) => state.error)
  const includeAnalysis = useForecastStore((state) => state.includeAnalysis)
  const selectSegment = useForecastStore((state) => state.selectSegment)
  const fetchForecast = useForecastStore((state) => state.fetchForecast)
  const clearSelection = useForecastStore((state) => state.clearSelection)
  const toast = useToast((state) => state.toast)

  const loadSegmentForecast = useCallback(
    async (segment: ForecastSegmentInfo) => {
      selectSegment(segment)
      try {
        await fetchForecast({ includeAnalysis: true })
      } catch (error) {
        toast({
          title: '预报曲线加载失败',
          description: getApiErrorMessage(error, '获取预报曲线失败'),
          variant: 'destructive',
        })
      }
    },
    [fetchForecast, selectSegment, toast],
  )

  const retryForecast = useCallback(() => {
    void fetchForecast({ includeAnalysis: true }).catch((error) => {
      toast({
        title: '预报曲线加载失败',
        description: getApiErrorMessage(error, '获取预报曲线失败'),
        variant: 'destructive',
      })
    })
  }, [fetchForecast, toast])

  return (
    <div
      className={cn(
        'grid min-h-[calc(100vh-7rem)] gap-4 lg:h-[calc(100vh-7rem)]',
        selectedSegment ? 'lg:grid-cols-[minmax(0,1fr)_24rem]' : 'lg:grid-cols-1',
      )}
    >
      <section
        className="min-h-[32rem] overflow-hidden rounded-lg border border-border bg-panel lg:min-h-0"
        aria-label="河网地图"
      >
        <MapView
          className="h-full"
          selectedSegmentId={selectedSegment?.segmentId}
          onSegmentSelect={(segment) => void loadSegmentForecast(segment)}
          onClearSelection={clearSelection}
        />
      </section>

      {selectedSegment ? (
        <ForecastPanel
          segment={selectedSegment}
          forecastData={forecastData}
          loading={loading}
          error={error}
          includeAnalysis={includeAnalysis}
          onClose={clearSelection}
          onRetry={retryForecast}
        />
      ) : null}
    </div>
  )
}
