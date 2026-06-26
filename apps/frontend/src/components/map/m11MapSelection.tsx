import { Popup } from 'react-map-gl/maplibre'
import type { ReactNode } from 'react'

export interface M11MapPopupSlot {
  longitude: number
  latitude: number
  content: ReactNode
  onClose?: () => void
}

export type M11SelectedSegmentMapState = 'idle' | 'selected-layer' | 'unavailable'

export function resolveM11SelectedSegmentMapState({
  selectedSegmentId,
  hasSelectedSegmentGeometry,
  hasRenderableOverlay,
  hasBasinRiverFeatures,
}: {
  selectedSegmentId?: string | null
  hasSelectedSegmentGeometry: boolean
  hasRenderableOverlay: boolean
  hasBasinRiverFeatures: boolean
}): M11SelectedSegmentMapState {
  if (!selectedSegmentId) return 'idle'
  return hasSelectedSegmentGeometry || hasRenderableOverlay || hasBasinRiverFeatures ? 'selected-layer' : 'unavailable'
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

export function M11MapPopupSlotPrimitive({ popup }: { popup: M11MapPopupSlot | null }) {
  if (!popup) return null
  // popup anchor 不指定 → maplibre 按可用空间自动选边，高弹窗在视口边缘不被裁切。
  return (
    <Popup
      longitude={popup.longitude}
      latitude={popup.latitude}
      closeOnClick={false}
      onClose={popup.onClose}
      maxWidth="none"
    >
      {popup.content}
    </Popup>
  )
}
