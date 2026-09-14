export type M11SelectedSegmentMapState = 'idle' | 'selected-layer' | 'unavailable'

export function resolveM11SelectedSegmentMapState({
  selectedSegmentId,
  hasRenderableOverlay,
}: {
  selectedSegmentId?: string | null
  hasRenderableOverlay: boolean
}): M11SelectedSegmentMapState {
  if (!selectedSegmentId) return 'idle'
  return hasRenderableOverlay ? 'selected-layer' : 'unavailable'
}

export function m11SelectionDataAttributes({
  selectedSegmentId,
  selectedSegmentMapState,
  selectedStationId,
}: {
  selectedSegmentId?: string | null
  selectedSegmentMapState: M11SelectedSegmentMapState
  selectedStationId?: string | null
}) {
  return {
    'data-selected-segment-id': selectedSegmentId ?? '',
    'data-segment-highlight-hook': selectedSegmentMapState,
    'data-selected-segment-map-state': selectedSegmentMapState,
    'data-selected-station-id': selectedStationId ?? '',
  }
}
