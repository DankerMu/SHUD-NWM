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

export function MaplibreSourceStub({ children }: { children?: React.ReactNode }) {
  return <>{children}</>
}

export function MaplibreLayerStub() {
  return null
}

export function MaplibreMarkerStub({ children }: { children?: React.ReactNode }) {
  return <>{children}</>
}
