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

export const MaplibreMapStub = forwardRef<unknown, { children?: React.ReactNode }>(function MaplibreMapStub(props, ref) {
  useImperativeHandle(ref, () => maplibreStubState.current)
  return <div data-testid="mock-maplibre-map">{props.children}</div>
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
