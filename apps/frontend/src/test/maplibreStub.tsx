import { forwardRef, useImperativeHandle } from 'react'

/** Mutable state shared between the vitest mock of react-map-gl/maplibre and tests. */
export const maplibreStubState: { current: unknown } = { current: null }

export function installMaplibreStubMap(map: unknown) {
  // If the caller already passed a MapRef-shaped object (has getMap), keep it.
  if (map && typeof map === 'object' && 'getMap' in (map as Record<string, unknown>)) {
    maplibreStubState.current = map
    return
  }
  // Otherwise wrap: the react-map-gl MapRef shape is ref.getMap() -> maplibre map.
  maplibreStubState.current = { getMap: () => map }
}

type MaplibreMapStubProps = {
  children?: React.ReactNode
  onClick?: (event: unknown) => void
}

/**
 * Faithful ordinary-click seam: expose the production `onClick` so a test can
 * drive `handleM11MapClick` without going through the gated hook. Tests stash
 * the MapLibre-shaped event on `window.__nhmsOrdinaryMapClickEvent` so the
 * stub can forward exact layer/feature/lngLat arguments.
 */
export const MaplibreMapStub = forwardRef<unknown, MaplibreMapStubProps>(function MaplibreMapStub(props, ref) {
  useImperativeHandle(ref, () => maplibreStubState.current)
  return (
    <div data-testid="mock-maplibre-map">
      <button
        type="button"
        data-testid="mock-maplibre-ordinary-click"
        onClick={() => {
          const event = (window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent
          props.onClick?.(event)
        }}
      >
        ordinary-click
      </button>
      {props.children}
    </div>
  )
})

export function MaplibreControlStub() {
  return null
}

/**
 * `Source` 的观测面（fixture #2015 决策 5）：把 `type` / `url` 暴露成 DOM 属性，让
 * 「隐藏 ⇒ 没有 `type="image"` 的 source 元素 ⇒ 不发 PNG 请求」这条断言落在真正闸住
 * 请求的那个组件上，而不是靠 fetch spy（jsdom + stub 下本就不会有网络请求，那是空断言）。
 * 除多包一层 `div` 外仍是纯透传桩，不影响只查 `m11-map-surface` 属性的既有用例。
 */
export function MaplibreSourceStub({
  type,
  url,
  children,
}: {
  type?: string
  url?: string
  children?: React.ReactNode
}) {
  return (
    <div data-testid="maplibre-source" data-source-type={type} data-source-url={url ?? undefined}>
      {children}
    </div>
  )
}

/**
 * `Layer` 的观测面：`beforeId` 是本仓唯一无法从 `Source` 侧观测的 prop（决策 4 的
 * 「仅当河网 source 存在时才传」形状断言要读它），故这里也从 `null` 升级为可断言的 DOM 节点。
 */
export function MaplibreLayerStub({ id, beforeId }: { id?: string; beforeId?: string }) {
  return <div data-testid="maplibre-layer" data-layer-id={id} data-layer-before-id={beforeId ?? undefined} />
}

export function MaplibreMarkerStub({ children }: { children?: React.ReactNode }) {
  return <>{children}</>
}
