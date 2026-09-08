import { describe, expect, it } from 'vitest'

import {
  M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED,
  M11_PRECIP_NOTICE_INDEX_ERROR,
  M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE,
  M11_PRECIP_NOTICE_WINDOW_INCOMPLETE,
  resolveM11PrecipOverlay,
  type M11PrecipOverlayInput,
} from '@/components/map/m11PrecipOverlay'
import { m11SourceCycleKey, type PrecipIndex, type PrecipIndexState } from '@/stores/overviewData'
import { precipIndex } from '@/test/overviewDataFixture'

const CYCLE_SECONDS = '2026-05-18T00:00:00Z'
/** `LayerState.currentValidTime` 是 JS 毫秒形；index 的 `valid_times[]` 是后端的秒精度形。 */
const VALID_TIME_MS = '2026-05-18T03:00:00.000Z'
const VALID_TIME_SECONDS = '2026-05-18T03:00:00Z'

/** 共享 fixture（`precipIndex`）的 `bounds` / `valid_times` 是本文件唯一的 index 真值来源。 */
function indexState(overrides: Partial<PrecipIndex> = {}): PrecipIndexState {
  return { status: 'available', index: { ...(precipIndex as PrecipIndex), ...overrides } }
}

function input(overrides: Partial<M11PrecipOverlayInput> = {}): M11PrecipOverlayInput {
  const source = overrides.concreteSource === undefined ? 'gfs' : overrides.concreteSource
  const cycle = overrides.cycle === undefined ? CYCLE_SECONDS : overrides.cycle
  return {
    precip: true,
    concreteSource: source,
    cycle,
    validTime: VALID_TIME_MS,
    precipIndexByCycle:
      source && cycle ? { [m11SourceCycleKey(source, cycle)]: indexState() } : {},
    ...overrides,
  }
}

describe('resolveM11PrecipOverlay visible triple', () => {
  it('builds the seconds-precision PNG url from a millisecond-form valid time', () => {
    const model = resolveM11PrecipOverlay(input())
    // `state.validTime` 传的是 `…T03:00:00.000Z`：URL 里必须是秒精度，否则后端 404。
    expect(model.url).toBe(`/api/v1/precip/gfs/${CYCLE_SECONDS}/${VALID_TIME_SECONDS}.png`)
    expect(model.hiddenReason).toBeNull()
    expect(model.notice).toBeNull()
  })

  it('changes the url when any of source / cycle / validTime changes', () => {
    const base = resolveM11PrecipOverlay(input()).url
    const otherCycle = '2026-05-17T12:00:00Z'
    const otherValidTime = '2026-05-18T00:00:00.000Z'

    const bySource = resolveM11PrecipOverlay(
      input({
        concreteSource: 'ifs',
        precipIndexByCycle: { [m11SourceCycleKey('ifs', CYCLE_SECONDS)]: indexState() },
      }),
    ).url
    const byCycle = resolveM11PrecipOverlay(
      input({
        cycle: otherCycle,
        precipIndexByCycle: { [m11SourceCycleKey('gfs', otherCycle)]: indexState() },
      }),
    ).url
    const byValidTime = resolveM11PrecipOverlay(input({ validTime: otherValidTime })).url

    expect(base).toBe(`/api/v1/precip/gfs/${CYCLE_SECONDS}/${VALID_TIME_SECONDS}.png`)
    expect(bySource).toBe(`/api/v1/precip/ifs/${CYCLE_SECONDS}/${VALID_TIME_SECONDS}.png`)
    expect(byCycle).toBe(`/api/v1/precip/gfs/${otherCycle}/${VALID_TIME_SECONDS}.png`)
    expect(byValidTime).toBe(`/api/v1/precip/gfs/${CYCLE_SECONDS}/2026-05-18T00:00:00Z.png`)
    expect(new Set([base, bySource, byCycle, byValidTime]).size).toBe(4)
  })

  it('derives the NW/NE/SE/SW image corners from index bounds [w, s, e, n]', () => {
    const model = resolveM11PrecipOverlay(input())
    expect(precipIndex.bounds).toEqual([73, 18, 135, 54])
    expect(model.coordinates).toEqual([
      [73, 54],
      [135, 54],
      [135, 18],
      [73, 18],
    ])
  })

  it('names the ifs mirror route when the resolved concrete source is ifs', () => {
    const model = resolveM11PrecipOverlay(
      input({
        concreteSource: 'ifs',
        precipIndexByCycle: { [m11SourceCycleKey('ifs', CYCLE_SECONDS)]: indexState() },
      }),
    )
    expect(model.url).toContain('/api/v1/precip/ifs/')
    expect(model.hiddenReason).toBeNull()
  })
})

describe('resolveM11PrecipOverlay hidden ladder', () => {
  it('hides silently when the overlay toggle is off', () => {
    const model = resolveM11PrecipOverlay(input({ precip: false }))
    expect(model.hiddenReason).toBe('disabled')
    expect(model.url).toBeNull()
    expect(model.coordinates).toBeNull()
    expect(model.notice).toBeNull()
  })

  it('reports no_concrete_source for compare even when the cycle is null and the key is absent', () => {
    // 全国 `compare`：同时也没有活动对、键也缺席——但阶梯第 2 臂先命中，用户看到提示 A
    // 而不是沉默的 index_pending。这是短路顺序（不是枚举声明顺序）的判别用例。
    const model = resolveM11PrecipOverlay(
      input({ concreteSource: null, cycle: null, precipIndexByCycle: {} }),
    )
    expect(model.hiddenReason).toBe('no_concrete_source')
    expect(model.notice).toBe(M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE)
    expect(model.url).toBeNull()
  })

  it.each([
    ['cycle unresolved', { cycle: null }],
    ['validTime unresolved', { validTime: null }],
    ['index record absent', { precipIndexByCycle: {} }],
  ])('reports index_pending without a notice when %s', (_label, overrides) => {
    const model = resolveM11PrecipOverlay(input(overrides as Partial<M11PrecipOverlayInput>))
    expect(model.hiddenReason).toBe('index_pending')
    expect(model.notice).toBeNull()
    expect(model.url).toBeNull()
  })

  it('reports index_error for a failed index fetch', () => {
    const model = resolveM11PrecipOverlay(
      input({ precipIndexByCycle: { [m11SourceCycleKey('gfs', CYCLE_SECONDS)]: { status: 'error' } } }),
    )
    expect(model.hiddenReason).toBe('index_error')
    expect(model.notice).toBe(M11_PRECIP_NOTICE_INDEX_ERROR)
    expect(model.url).toBeNull()
  })

  it('reports index_error when an available index carries malformed bounds', () => {
    const model = resolveM11PrecipOverlay(
      input({
        precipIndexByCycle: { [m11SourceCycleKey('gfs', CYCLE_SECONDS)]: indexState({ bounds: [73, 18, 135] }) },
      }),
    )
    expect(model.hiddenReason).toBe('index_error')
    expect(model.notice).toBe(M11_PRECIP_NOTICE_INDEX_ERROR)
    expect(model.coordinates).toBeNull()
  })

  it('reports cycle_not_mirrored for a 404 PRECIP_CYCLE_NOT_MIRRORED cycle', () => {
    const model = resolveM11PrecipOverlay(
      input({ precipIndexByCycle: { [m11SourceCycleKey('gfs', CYCLE_SECONDS)]: { status: 'not_mirrored' } } }),
    )
    expect(model.hiddenReason).toBe('cycle_not_mirrored')
    expect(model.notice).toBe(M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)
    expect(model.url).toBeNull()
  })

  it('reports window_incomplete when the valid time is outside valid_times at seconds precision', () => {
    const model = resolveM11PrecipOverlay(
      input({
        validTime: '2026-05-18T06:00:00.000Z',
        precipIndexByCycle: {
          [m11SourceCycleKey('gfs', CYCLE_SECONDS)]: indexState({
            valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z'],
          }),
        },
      }),
    )
    expect(model.hiddenReason).toBe('window_incomplete')
    expect(model.notice).toBe(M11_PRECIP_NOTICE_WINDOW_INCOMPLETE)
    expect(model.url).toBeNull()
  })

  it('matches valid_times at seconds precision rather than by raw string equality', () => {
    // index 是秒形、`LayerState` 是毫秒形：朴素 `includes` 会把这一格误判成 window_incomplete。
    const model = resolveM11PrecipOverlay(
      input({
        validTime: VALID_TIME_MS,
        precipIndexByCycle: {
          [m11SourceCycleKey('gfs', CYCLE_SECONDS)]: indexState({ valid_times: [VALID_TIME_SECONDS] }),
        },
      }),
    )
    expect(model.hiddenReason).toBeNull()
    expect(model.url).toContain(`/${VALID_TIME_SECONDS}.png`)
  })
})

describe('M11 precipitation notices', () => {
  it('keeps the four hidden-reason notices pairwise distinct', () => {
    const notices = [
      M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE,
      M11_PRECIP_NOTICE_INDEX_ERROR,
      M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED,
      M11_PRECIP_NOTICE_WINDOW_INCOMPLETE,
    ]
    for (const notice of notices) expect(notice.length).toBeGreaterThan(0)
    for (let i = 0; i < notices.length; i += 1) {
      for (let j = i + 1; j < notices.length; j += 1) {
        expect(notices[i]).not.toBe(notices[j])
      }
    }
  })
})
