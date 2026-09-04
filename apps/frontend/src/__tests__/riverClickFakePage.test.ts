import { describe, expect, it, vi } from 'vitest'

import { closeRiverClickPanelInPage } from '../../playwright.river-click-lane-attempt'
import { makeFakePage, makeFakePageState } from '../test/riverClickFakePage'

describe('river-click fake page close seam (identity, not source-string)', () => {
  it('routes only the production closeRiverClickPanelInPage identity to closeImpl; a source-marker twin is not diverted', async () => {
    const state = makeFakePageState()
    const closeImpl = vi.fn(() => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true }))
    state.closeImpl = closeImpl
    const page = makeFakePage(state)
    const captured = { hook: { marker: 'hook' }, map: { marker: 'map' }, budgetMs: 10, pollMs: 1 }

    await page.evaluate(closeRiverClickPanelInPage as never, captured)
    expect(closeImpl).toHaveBeenCalledTimes(1)
    expect(closeImpl).toHaveBeenCalledWith(captured)

    const twin = function closeRiverClickPanelInPage(arg: unknown) {
      void arg
      void document.querySelector('[data-testid="m11-river-forecast-panel"]')
      return { closed: 'twin', mapPresent: false, mapSame: false, hookSame: false }
    }
    Object.defineProperty(twin, 'name', { value: 'closeRiverClickPanelInPage' })
    expect(String(twin)).toContain('m11-river-forecast-panel')
    expect(twin.name).toBe('closeRiverClickPanelInPage')
    expect(twin).not.toBe(closeRiverClickPanelInPage)
    const twinResult = await page.evaluate(twin as never, captured)
    expect(closeImpl).toHaveBeenCalledTimes(1)
    expect(twinResult).toEqual({ closed: 'twin', mapPresent: false, mapSame: false, hookSame: false })
  })
})
