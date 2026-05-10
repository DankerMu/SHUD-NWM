import { useForecastStore } from '@/stores/forecast'
import type { ForecastData } from '@/stores/forecast'

const scenarioOptions = ['GFS', 'IFS']

function hasForecastSource(data: ForecastData | null, source: string) {
  return data?.series.some((series) => {
    if (series.isAnalysis) return false
    return `${series.source ?? ''} ${series.scenario}`.toUpperCase().includes(source)
  })
}

export function ScenarioSelector() {
  const selectedScenarios = useForecastStore((state) => state.selectedScenarios)
  const forecastData = useForecastStore((state) => state.forecastData)
  const loading = useForecastStore((state) => state.loading)
  const includeAnalysis = useForecastStore((state) => state.includeAnalysis)
  const toggleScenario = useForecastStore((state) => state.toggleScenario)
  const fetchForecast = useForecastStore((state) => state.fetchForecast)

  const ifsUnavailable =
    selectedScenarios.includes('IFS') && Boolean(forecastData) && !loading && !hasForecastSource(forecastData, 'IFS')

  const handleScenarioChange = (scenario: string) => {
    toggleScenario(scenario)
    void fetchForecast({ includeAnalysis }).catch(() => undefined)
  }

  return (
    <fieldset className="rounded-md border border-border bg-background/60 p-3">
      <legend className="px-1 text-xs font-medium text-muted">Scenario</legend>
      <div className="flex flex-wrap items-center gap-4">
        {scenarioOptions.map((scenario) => {
          const checked = selectedScenarios.includes(scenario)
          const disabled = checked && selectedScenarios.length === 1

          return (
            <label key={scenario} className="inline-flex min-h-8 items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className="size-4 rounded border-border accent-[#2266cc]"
                checked={checked}
                disabled={disabled}
                onChange={() => handleScenarioChange(scenario)}
                aria-label={`${scenario} scenario`}
              />
              <span className="font-medium">{scenario}</span>
              {scenario === 'IFS' && ifsUnavailable ? (
                <span className="text-xs text-muted">(暂无数据)</span>
              ) : null}
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}
