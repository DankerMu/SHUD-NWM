import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { M11MapStatusOverlays } from '@/components/map/m11MapRuntime'

function renderStatusOverlays(basinBoundaryOverlayEnabled: boolean) {
  return render(
    <M11MapStatusOverlays
      loading={false}
      boundaryLoading={false}
      basinBoundaryOverlayEnabled={basinBoundaryOverlayEnabled}
      basinCount={1}
      basinFeatureCount={0}
      skippedBasinGeometryCount={0}
      unavailableReason={null}
      basinRiverUnavailableReason={null}
      selectedSegmentMapState="idle"
      selectedSegmentUnavailableReason={null}
      mapSourceError={null}
    />,
  )
}

describe('M11MapStatusOverlays', () => {
  it('does not report a missing basin boundary when the boundary overlay is intentionally disabled', () => {
    renderStatusOverlays(false)

    expect(screen.queryByTestId('m11-basin-layer-unavailable')).not.toBeInTheDocument()
  })

  it('keeps the boundary unavailable notice for an enabled overlay with no visible features', () => {
    renderStatusOverlays(true)

    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('当前没有可见流域边界。')
  })
})
