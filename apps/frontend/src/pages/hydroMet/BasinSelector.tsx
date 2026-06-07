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
  /** Invoked with the chosen basin_id, or null to fall back to the backend default basin. */
  onSelect: (basinId: string | null) => void
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
      {state.kind === 'loaded' && state.basins.length === 0 ? (
        <span className="text-xs text-neutral-600" data-testid="hydro-met-basin-empty">
          暂无可展示流域（后端未返回任何 display 流域）。
        </span>
      ) : null}
      {state.kind === 'loaded' && state.basins.length > 0 ? (
        <select
          aria-label="水文气象流域"
          data-testid="hydro-met-basin-select"
          className={cn(
            'h-9 cursor-pointer rounded border border-neutral-300 bg-white px-2 text-sm text-neutral-900',
          )}
          value={selectedBasinId ?? ''}
          onChange={(event) => {
            const next = event.target.value
            // Empty value = explicit fall-back to backend default basin.
            onSelect(next ? next : null)
          }}
        >
          {/* 始终保留默认流域空选项，允许从任意流域显式回退后端缺省。 */}
          <option value="">默认流域</option>
          {/* 陈旧/无效 id 不在已加载列表时渲染占位 option，避免受控 select 显示空白错位；不伪造身份。 */}
          {selectedBasinId !== null && !state.basins.some((basin) => basin.basin_id === selectedBasinId) ? (
            <option value={selectedBasinId}>未知流域: {selectedBasinId}</option>
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
