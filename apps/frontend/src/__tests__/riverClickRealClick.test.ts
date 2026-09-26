import { describe, expect, it, vi } from 'vitest'

import {
  classifyRiverClickLocateOutcome,
  classifyRiverClickPointerCapture,
  riverClickLocateScript,
  runRiverClickAttempt,
  RIVER_CLICK_ARM_CAPTURE_SCRIPT,
  RIVER_CLICK_TAKE_CAPTURE_SCRIPT,
} from '../../playwright.river-click-lane'
import { classifyRiverClickPointerCapture as classifyAttemptCapture } from '../../playwright.river-click-lane-attempt'
import {
  createRiverClickPointerCapture,
  type RiverClickHookCanvas,
  type RiverClickPointerEventLike,
} from '../lib/riverClickEvidence/hook'
import { FAKE_LOCATED as LOCATED, fakeLocateThenClick as locateThenClick, makeFakePage, type RiverClickFakePageState } from '../test/riverClickFakePage'
import { config, emitSeriesPair, freshState, isLocate, makeIdentity } from '../test/riverClickLaneFixtures'

type FakePageState = RiverClickFakePageState

/**
 * D2 real-click attempt (#1970 batch Q): per attempt the lane arms response
 * observation and the page-side pointer capture, locates (no dispatch), clicks
 * the located viewport point exactly once with page.mouse.click, and takes t0
 * from the captured trusted pointer-down. A missing/untrusted/duplicate/
 * displaced capture is CLICK_DISPATCH_INVALID; a hook rejection keeps its
 * closed code in the HOOK_SELECTION_FAILED message.
 */
describe('river-click lane attempt: one real click, t0 from the trusted pointer capture', () => {
  function completeState(options: { t0?: number; t1?: number } = {}) {
    const state = freshState()
    state.evaluateImpl = (text) => {
      if (isLocate(text)) {
        return locateThenClick(state, {
          t0: options.t0 ?? 1000,
          onClick: () => {
            emitSeriesPair(state, 'GFS')
            emitSeriesPair(state, 'IFS')
          },
        })
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return options.t1 ?? 1400
      return undefined
    }
    state.closeImpl = () => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true })
    return state
  }

  const run = (state: FakePageState) =>
    runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )

  it('arms observation and capture, locates, clicks the located point exactly once, then takes t0 from the capture timeStamp', async () => {
    const state = completeState({ t0: 1234.25, t1: 1500 })
    const attempt = await run(state)
    expect(attempt.ok).toBe(true)
    if (!attempt.ok) throw new Error(`attempt must succeed: ${attempt.failure.code}`)
    expect(attempt.t0Ms).toBe(1234.25)
    expect(attempt.t1Ms).toBe(1500)
    expect(state.mouseClicks).toEqual([{ x: 412.5, y: 318 }])
    const arm = state.evaluateNames.indexOf(RIVER_CLICK_ARM_CAPTURE_SCRIPT)
    const locate = state.evaluateNames.findIndex((name) => isLocate(name))
    const click = state.evaluateNames.indexOf('mouse-click')
    const take = state.evaluateNames.indexOf(RIVER_CLICK_TAKE_CAPTURE_SCRIPT)
    expect(arm).toBeGreaterThanOrEqual(0)
    expect(arm).toBeLessThan(locate)
    expect(locate).toBeLessThan(click)
    expect(click).toBeLessThan(take)
    // The locate script carries exactly the preflight input.
    expect(state.evaluateNames[locate]).toBe(riverClickLocateScript(makeIdentity()))
  })

  it('never invokes a hook dispatch: the only page scripts touching the hook are readiness-free arm/locate/take', async () => {
    const state = completeState()
    await run(state)
    const hookScripts = state.evaluateNames.filter((name) => name.includes('__nhmsRiverClickEvidence.'))
    expect(hookScripts).toEqual([RIVER_CLICK_ARM_CAPTURE_SCRIPT, riverClickLocateScript(makeIdentity()), RIVER_CLICK_TAKE_CAPTURE_SCRIPT])
    expect(state.evaluateNames.join('\n')).not.toMatch(/onOverlayClick|dispatchEvent/)
  })

  it('fails CLICK_DISPATCH_INVALID when the captured pointer-down is more than 2 CSS px from the located point on either axis', async () => {
    for (const [dx, dy, ok] of [[2, -2, true], [2.5, 0, false], [0, -2.01, false]] as const) {
      const state = completeState()
      state.evaluateImpl = ((inner) => (text: string) => {
        if (text === RIVER_CLICK_TAKE_CAPTURE_SCRIPT) {
          return { timeStamp: 1000, clientX: LOCATED.clientX + dx, clientY: LOCATED.clientY + dy, isTrusted: true }
        }
        return inner(text)
      })(state.evaluateImpl)
      const attempt = await run(state)
      expect(attempt.ok, `displacement ${dx},${dy}`).toBe(ok)
      if (!attempt.ok) {
        expect(attempt.failure.code).toBe('CLICK_DISPATCH_INVALID')
        expect(attempt.failure.message).toBe('pointer capture is displaced more than 2 CSS px from the located point')
      }
    }
  })

  it('fails CLICK_DISPATCH_INVALID for an untrusted, missing, duplicate or malformed capture', async () => {
    const cases: Array<[string, unknown, string]> = [
      ['untrusted record', { timeStamp: 1000, clientX: LOCATED.clientX, clientY: LOCATED.clientY, isTrusted: false }, 'pointer capture is not a trusted pointer event'],
      ['missing', { error: 'HOOK_POINTER_MISSING' }, 'pointer capture HOOK_POINTER_MISSING'],
      ['invalid (duplicate/untrusted observed)', { error: 'HOOK_POINTER_INVALID' }, 'pointer capture HOOK_POINTER_INVALID'],
      ['unclassified error', { error: 'EVIL' }, 'pointer capture returned an unclassified error'],
      ['non-finite timestamp', { timeStamp: Number.NaN, clientX: LOCATED.clientX, clientY: LOCATED.clientY, isTrusted: true }, 'pointer capture record is malformed'],
      ['no record', null, 'pointer capture returned no record'],
    ]
    for (const [label, raw, message] of cases) {
      const state = completeState()
      state.evaluateImpl = ((inner) => (text: string) => (text === RIVER_CLICK_TAKE_CAPTURE_SCRIPT ? raw : inner(text)))(state.evaluateImpl)
      const attempt = await run(state)
      expect(attempt.ok, label).toBe(false)
      if (!attempt.ok) {
        expect(attempt.failure.code, label).toBe('CLICK_DISPATCH_INVALID')
        expect(attempt.failure.message, label).toBe(message)
        expect(attempt.rendered).toEqual(makeIdentity().requestedFeature)
      }
    }
  })

  it('a click that never reaches the armed capture (e.g. the capture was not armed) is HOOK_POINTER_MISSING -> CLICK_DISPATCH_INVALID', async () => {
    const state = completeState()
    // The page-side arm silently fails to arm (the fake returns a value, so its capture stand-in is never armed).
    state.evaluateImpl = ((inner) => (text: string) => (text === RIVER_CLICK_ARM_CAPTURE_SCRIPT ? null : inner(text)))(state.evaluateImpl)
    const attempt = await run(state)
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure).toMatchObject({ code: 'CLICK_DISPATCH_INVALID', message: 'pointer capture HOOK_POINTER_MISSING' })
    expect(state.mouseClicks).toHaveLength(1)
  })

  it('a thrown arm or a failed real click is CLICK_DISPATCH_INVALID; no click happens after a failed arm', async () => {
    const armThrows = completeState()
    armThrows.evaluateImpl = ((inner) => (text: string) => {
      if (text === RIVER_CLICK_ARM_CAPTURE_SCRIPT) throw new Error('raw page error')
      return inner(text)
    })(armThrows.evaluateImpl)
    const armAttempt = await run(armThrows)
    expect(armAttempt.ok).toBe(false)
    if (!armAttempt.ok) expect(armAttempt.failure).toMatchObject({ code: 'CLICK_DISPATCH_INVALID', message: 'pointer capture could not be armed' })
    expect(armThrows.mouseClicks).toHaveLength(0)

    const clickFails = completeState()
    const page = makeFakePage(clickFails)
    ;(page.mouse.click as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('raw input error'))
    const clickAttempt = await runRiverClickAttempt({ config: config(), page }, makeIdentity(), makeIdentity().requestedFeature, {
      attemptDeadlineMs: 500,
      pollMs: 2,
      quietMs: 10,
    })
    expect(clickAttempt.ok).toBe(false)
    if (!clickAttempt.ok) {
      expect(clickAttempt.failure).toMatchObject({ code: 'CLICK_DISPATCH_INVALID', message: 'real mouse click failed' })
      expect(clickAttempt.failure.message).not.toContain('raw input error')
    }
  })

  it('propagates a closed hook rejection code into the HOOK_SELECTION_FAILED message and redacts anything else, without clicking', async () => {
    const cases: Array<[unknown, string]> = [
      [{ ok: false, code: 'HOOK_FEATURE_MISMATCH' }, 'hook HOOK_FEATURE_MISMATCH'],
      [{ ok: false, code: 'HOOK_POINT_OCCLUDED' }, 'hook HOOK_POINT_OCCLUDED'],
      [{ ok: false, code: 'HOOK_MAP_TIMEOUT' }, 'hook HOOK_MAP_TIMEOUT'],
      [{ ok: false, code: 'secret-token-abc' }, 'hook rejected with an unclassified code'],
      [{ ok: false, code: null }, 'hook rejected with an unclassified code'],
      [{ ok: true, value: { ...LOCATED, clientX: Number.POSITIVE_INFINITY } }, 'locateRenderedRiver resolved without identities and a finite client point'],
      [{ ok: true, value: { clientX: 1, clientY: 2 } }, 'locateRenderedRiver resolved without identities and a finite client point'],
      [undefined, 'hook locate returned no outcome'],
    ]
    for (const [outcome, message] of cases) {
      const state = completeState()
      state.evaluateImpl = ((inner) => (text: string) => (isLocate(text) ? outcome : inner(text)))(state.evaluateImpl)
      const attempt = await run(state)
      expect(attempt.ok).toBe(false)
      if (!attempt.ok) {
        expect(attempt.failure.code).toBe('HOOK_SELECTION_FAILED')
        expect(attempt.failure.message).toBe(message)
        expect(attempt.rendered).toBeNull()
      }
      expect(state.mouseClicks).toHaveLength(0)
    }
  })

  it('the in-page locate wrapper turns a rejection object into {ok:false, code} (page.evaluate would drop a thrown object)', async () => {
    const hook = {
      locateRenderedRiver: vi.fn(() => Promise.reject({ code: 'HOOK_POINT_OCCLUDED', message: 'x' })),
    }
    ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = hook
    try {
      const outcome = await (0, eval)(riverClickLocateScript(makeIdentity()))
      expect(outcome).toEqual({ ok: false, code: 'HOOK_POINT_OCCLUDED' })
      expect(hook.locateRenderedRiver).toHaveBeenCalledWith({
        bbox: makeIdentity().bbox,
        anchor: makeIdentity().anchor,
        basinId: 'basins_qhh',
        riverSegmentId: 'seg-001',
        basinVersionId: 'bv-001',
        riverNetworkVersionId: 'rn-001',
      })
      hook.locateRenderedRiver.mockImplementationOnce(() => Promise.resolve(LOCATED) as never)
      expect(await (0, eval)(riverClickLocateScript(makeIdentity()))).toEqual({ ok: true, value: LOCATED })
      hook.locateRenderedRiver.mockImplementationOnce(() => Promise.reject(new Error('raw')) as never)
      expect(await (0, eval)(riverClickLocateScript(makeIdentity()))).toEqual({ ok: false, code: null })
    } finally {
      delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
    }
  })

  it('classifyRiverClickPointerCapture accepts exactly the 2-px boundary and a trusted finite record', () => {
    const located = { clientX: 100, clientY: 200 }
    expect(classifyRiverClickPointerCapture({ timeStamp: 5, clientX: 102, clientY: 198, isTrusted: true }, located)).toEqual({ ok: true, timeStamp: 5 })
    expect(classifyRiverClickPointerCapture({ timeStamp: 5, clientX: 102.001, clientY: 200, isTrusted: true }, located).ok).toBe(false)
    expect(classifyRiverClickLocateOutcome({ ok: true, value: LOCATED })).toEqual({ ok: true, located: LOCATED })
  })
})

/**
 * 2F.4: the page-side capture's REAL `take()` output, from a listener installed
 * on a jsdom `<canvas>`, feeds the lane's classifier unchanged. jsdom cannot
 * produce `isTrusted === true` (the property is an own, non-configurable
 * [LegacyUnforgeable] attribute, so `Object.defineProperty` throws). The
 * untrusted case is therefore a fully real `dispatchEvent`; the trusted case
 * invokes the listener the capture registered on the real canvas directly, with
 * the fields of a real jsdom `pointerdown` MouseEvent and `isTrusted: true`.
 */
describe('river-click capture -> lane classifier round-trip (real createRiverClickPointerCapture().take())', () => {
  function armedCanvas() {
    const canvas = document.createElement('canvas')
    document.body.appendChild(canvas)
    const addSpy = vi.spyOn(canvas, 'addEventListener')
    const capture = createRiverClickPointerCapture({ getCanvas: () => canvas as unknown as RiverClickHookCanvas })
    capture.arm()
    expect(addSpy).toHaveBeenCalledWith('pointerdown', expect.any(Function), { capture: true })
    const listener = addSpy.mock.calls[0][1] as unknown as (event: RiverClickPointerEventLike) => void
    const trustedDown = (clientX: number, clientY: number) => {
      const real = new MouseEvent('pointerdown', { clientX, clientY, bubbles: true })
      listener({ isTrusted: true, timeStamp: real.timeStamp, clientX: real.clientX, clientY: real.clientY, target: canvas })
      return real.timeStamp
    }
    return { canvas, capture, trustedDown }
  }

  it('accepts a single trusted capture within 2 CSS px and returns its timeStamp as t0', () => {
    const { canvas, capture, trustedDown } = armedCanvas()
    try {
      const timeStamp = trustedDown(142, 88)
      const raw = capture.take()
      expect(raw).toEqual({ timeStamp, clientX: 142, clientY: 88, isTrusted: true })
      expect(classifyAttemptCapture(raw, { clientX: 140, clientY: 90 })).toEqual({ ok: true, timeStamp })
    } finally {
      canvas.remove()
    }
  })

  it('rejects a trusted capture displaced more than 2 CSS px', () => {
    const { canvas, capture, trustedDown } = armedCanvas()
    try {
      trustedDown(143, 90)
      expect(classifyAttemptCapture(capture.take(), { clientX: 140, clientY: 90 })).toEqual({
        ok: false,
        message: 'pointer capture is displaced more than 2 CSS px from the located point',
      })
    } finally {
      canvas.remove()
    }
  })

  it('rejects a real untrusted dispatchEvent and a missing capture with the closed hook code in the message', () => {
    const { canvas, capture } = armedCanvas()
    try {
      canvas.dispatchEvent(new MouseEvent('pointerdown', { clientX: 140, clientY: 90, bubbles: true }))
      expect(classifyAttemptCapture(capture.take(), { clientX: 140, clientY: 90 })).toEqual({ ok: false, message: 'pointer capture HOOK_POINTER_INVALID' })
      capture.arm()
      expect(classifyAttemptCapture(capture.take(), { clientX: 140, clientY: 90 })).toEqual({ ok: false, message: 'pointer capture HOOK_POINTER_MISSING' })
    } finally {
      canvas.remove()
    }
  })
})
