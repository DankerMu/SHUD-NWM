import { useEffect, useState } from 'react'
import { Loader2, Mountain } from 'lucide-react'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { cn } from '@/lib/cn'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

type Basin = components['schemas']['Basin']

type BasinSelectorState =
  | { kind: 'loading' }
  | { kind: 'loaded'; basins: Basin[] }
  | { kind: 'error'; message: string }

async function loadDisplayBasins(): Promise<Basin[]> {
  const { data, error } = await client.GET('/api/v1/basins', {
    params: { query: { has_display_product: true } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '流域列表加载失败'))
  const basins = unwrapApiData<Basin[]>(data, '流域列表加载失败')
  if (!Array.isArray(basins)) throw new Error('流域列表响应不完整')
  return basins
}

interface BasinSelectorProps {
  /** Currently selected basin_id from URL/state; null means backend default basin. */
  selectedBasinId: string | null
  /** Invoked with the chosen basin_id when the user switches basins. */
  onSelect: (basinId: string) => void
}

export function BasinSelector({ selectedBasinId, onSelect }: BasinSelectorProps) {
  const [state, setState] = useState<BasinSelectorState>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ kind: 'loading' })
    void loadDisplayBasins().then(
      (basins) => {
        if (!cancelled) setState({ kind: 'loaded', basins })
      },
      (error) => {
        if (!cancelled) {
          setState({
            kind: 'error',
            message: sanitizeHydroMetMessage(getApiErrorMessage(error, '流域列表加载失败'), '流域列表加载失败'),
          })
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="flex items-center gap-2" data-testid="hydro-met-basin-selector">
      <Mountain className="h-4 w-4 text-neutral-500" aria-hidden="true" />
      {state.kind === 'loading' ? (
        <span className="flex items-center gap-1 text-xs text-neutral-500" data-testid="hydro-met-basin-loading">
          <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
          加载流域…
        </span>
      ) : null}
      {state.kind === 'error' ? (
        <span className="text-xs text-danger" data-testid="hydro-met-basin-error">
          {state.message}
        </span>
      ) : null}
      {state.kind === 'loaded' ? (
        <select
          aria-label="水文气象流域"
          data-testid="hydro-met-basin-select"
          className={cn(
            'h-9 cursor-pointer rounded border border-neutral-300 bg-white px-2 text-sm text-neutral-900',
          )}
          value={selectedBasinId ?? ''}
          onChange={(event) => {
            const next = event.target.value
            if (next) onSelect(next)
          }}
        >
          {selectedBasinId === null ? (
            <option value="">默认流域</option>
          ) : null}
          {state.basins.map((basin) => (
            <option key={basin.basin_id} value={basin.basin_id}>
              {basin.basin_name}
            </option>
          ))}
        </select>
      ) : null}
    </div>
  )
}
