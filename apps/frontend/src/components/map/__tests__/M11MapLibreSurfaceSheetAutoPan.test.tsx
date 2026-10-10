import { StrictMode, type ReactElement } from 'react'
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface, type M11SheetAutoPan } from '@/components/map/M11MapLibreSurface'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'
import { installMaplibreStubMap, maplibreMapStubProps } from '@/test/maplibreStub'

/**
 * 抽屉自动平移与选中锚点属性的地图组件级用例（openspec mobile-responsive-display task 4.10，design.md D17）。
 * 用例 (s1)–(s10) 对应 tasks.md 里 #2811 的 Triage。
 *
 * `<Map>` 用共享桩；假地图在本文件自建（共享桩的假地图没有 `easeTo` / `resize` / `project` 等方法），并把
 * 全部相机方法调用按顺序记进 `calls`。`moveend` 经桩 `<Map>` 收到的 `onMoveEnd` prop 触发；假地图的
 * `resize()` 像真实地图库那样同步发一次 `moveend`。平移排在键变化后的下一帧，所以这里把
 * `requestAnimationFrame` 换成手动队列，由 `flushFrames()` 在 `act` 里执行；`cancelAnimationFrame` 把还没
 * 执行的那一帧从队列里摘掉。
 */
vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreControlStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
  return {
    default: MaplibreMapStub,
    Map: MaplibreMapStub,
    NavigationControl: MaplibreControlStub,
    ScaleControl: MaplibreControlStub,
    Source: MaplibreSourceStub,
    Layer: MaplibreLayerStub,
    Marker: MaplibreMarkerStub,
  }
})

const state: M11QueryState = {
  source: 'best',
  cycle: '2026-09-02T00:00:00Z',
  validTime: null,
  layer: 'discharge',
  metStations: false,
  precip: true,
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  segmentId: null,
  q: null,
}
const layers: LayerState[] = []

const CANVAS_ORIGIN = { left: 10, top: 20 }
const RIVER_ANCHOR: [number, number] = [100.5, 30.25]
const STATION_ANCHOR: [number, number] = [101.5, 31.75]
const SHEET_SIZE = { width: 390, height: 398 }
const CAMERA_METHODS = ['easeTo', 'flyTo', 'jumpTo', 'panTo', 'fitBounds', 'setCenter', 'setPadding', 'resize'] as const
type CameraMethod = (typeof CAMERA_METHODS)[number]

const RIVER_PAN: M11SheetAutoPan = { key: 'bottom|river|seg-1|100.5,30.25', side: 'bottom', kind: 'river' }
const RIVER_PAN_SIDE: M11SheetAutoPan = { key: 'right|river|seg-1|100.5,30.25', side: 'right', kind: 'river' }
const STATION_PAN: M11SheetAutoPan = { key: 'bottom|station|st-1|101.5,31.75', side: 'bottom', kind: 'station' }

type SurfaceProps = Partial<Parameters<typeof M11MapLibreSurface>[0]>
type GateWindow = { __NHMS_E2E_HOOKS__?: unknown }
const gateWindow = window as unknown as GateWindow

/** 桩 `<Map>` 最近一次渲染收到的相机事件 prop（共享桩把全部 prop 原样存着，类型里没列这两个）。 */
function mapEventProps() {
  return (maplibreMapStubProps.current ?? {}) as { onMoveEnd?: () => void; onLoad?: () => void }
}

function installFakeMap() {
  const calls: Array<{ method: CameraMethod; args: unknown[] }> = []
  const reentrantMoveEnds = { count: 0 }
  // 假相机：`project` 是经纬度的线性函数加一个平移量，测试改平移量来模拟相机移动。
  const camera = { shiftX: 0, shiftY: 0, center: { lng: 100.123456789, lat: 30.987654321 }, zoom: 9.87654321 }
  const canvas = {
    style: { cursor: '' },
    getBoundingClientRect: () => CANVAS_ORIGIN,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }
  const record = (method: CameraMethod) => (...args: unknown[]) => {
    calls.push({ method, args })
  }
  const map = {
    easeTo: record('easeTo'),
    flyTo: record('flyTo'),
    jumpTo: record('jumpTo'),
    panTo: record('panTo'),
    fitBounds: record('fitBounds'),
    setCenter: record('setCenter'),
    setPadding: record('setPadding'),
    // 真实地图库的 `resize()` 在空闲时同步发 `moveend`：这里照做，重入不得被当成触发。
    resize: (...args: unknown[]) => {
      calls.push({ method: 'resize', args })
      const onMoveEnd = mapEventProps().onMoveEnd
      if (!onMoveEnd) return
      reentrantMoveEnds.count += 1
      onMoveEnd()
    },
    project: ([lng, lat]: [number, number]) => ({ x: (lng - 100) * 100 + camera.shiftX, y: (lat - 30) * 100 + camera.shiftY }),
    getCenter: () => camera.center,
    getZoom: () => camera.zoom,
    getCanvas: () => canvas,
  }
  installMaplibreStubMap({ getMap: () => map })
  return {
    calls,
    camera,
    reentrantMoveEnds,
    names: () => calls.map((call) => call.method),
    eases: () => calls.filter((call) => call.method === 'easeTo').map((call) => call.args[0] as Record<string, unknown>),
  }
}

/** 假抽屉：jsdom 没有布局，包围盒直接钉在节点上。 */
function FakeSheet({ kind, size }: { kind: 'river' | 'station'; size: { width: number; height: number } }) {
  return (
    <aside
      data-m11-curve-window-kind={kind}
      ref={(node) => {
        if (node) node.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: size.width, bottom: size.height, ...size, toJSON: () => size })
      }}
    />
  )
}

interface HarnessOptions {
  props?: SurfaceProps
  /** 地图区里渲染哪种抽屉；null = 不渲染。 */
  sheet?: 'river' | 'station' | null
  sheetSize?: { width: number; height: number }
}

function harness({ props = {}, sheet = 'river', sheetSize = SHEET_SIZE }: HarnessOptions = {}): ReactElement {
  return (
    <section data-testid="m11-fullscreen-map">
      <M11MapLibreSurface state={state} layers={layers} loading={false} boundaryLoading={false} {...props} />
      {sheet ? <FakeSheet kind={sheet} size={sheetSize} /> : null}
    </section>
  )
}

const surface = () => screen.getByTestId('m11-map-surface')
const anchorAttributes = () => ({
  x: surface().getAttribute('data-selected-anchor-x'),
  y: surface().getAttribute('data-selected-anchor-y'),
})
const cameraAttributes = () => ({
  center: surface().getAttribute('data-camera-center'),
  zoom: surface().getAttribute('data-camera-zoom'),
})

describe('M11MapLibreSurface: sheet auto-pan and selected-anchor attributes (D17)', () => {
  let frames: FrameRequestCallback[] = []

  /** 执行已排队的帧回调（含回调里新排的），直到队列空。 */
  function flushFrames() {
    act(() => {
      while (frames.length > 0) frames.shift()!(0)
    })
  }

  function fireMoveEnd() {
    act(() => mapEventProps().onMoveEnd?.())
  }

  beforeEach(() => {
    frames = []
    // 每一帧一个唯一句柄；`cancelAnimationFrame` 按句柄把还没执行的回调从队列里摘掉（已执行的是空操作）。
    let lastFrameId = 0
    const callbacksById = new Map<number, FrameRequestCallback>()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      lastFrameId += 1
      callbacksById.set(lastFrameId, callback)
      frames.push(callback)
      return lastFrameId
    })
    vi.stubGlobal('cancelAnimationFrame', (frameId: number) => {
      const callback = callbacksById.get(frameId)
      const index = callback ? frames.indexOf(callback) : -1
      if (index >= 0) frames.splice(index, 1)
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete gateWindow.__NHMS_E2E_HOOKS__
    installMaplibreStubMap(null)
  })

  it('(s1) the key going from empty to non-empty eases exactly once: center = anchor, offset up by half the sheet height, and nothing but center / offset / duration', () => {
    const fake = installFakeMap()
    const view = render(harness())
    flushFrames()
    expect(fake.names()).toEqual([])

    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    // 平移排在下一帧：这次提交里还没有相机调用。
    expect(fake.eases()).toHaveLength(0)
    flushFrames()

    expect(fake.eases()).toHaveLength(1)
    const [options] = fake.eases()
    expect(Object.keys(options).sort()).toEqual(['center', 'duration', 'offset'])
    expect(options.center).toEqual(RIVER_ANCHOR)
    expect(options.offset).toEqual([0, -SHEET_SIZE.height / 2])
    expect(options.duration).toBeGreaterThan(0)
    expect(options.duration).toBeLessThanOrEqual(450)
    for (const forbidden of ['zoom', 'bearing', 'pitch', 'padding']) expect(options).not.toHaveProperty(forbidden)
    expect(fake.names()).not.toContain('setPadding')
  })

  it('(s2) re-renders with the same key (other props changing) add no camera call', () => {
    const fake = installFakeMap()
    const props: SurfaceProps = { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN }
    const view = render(harness({ props }))
    flushFrames()
    const after = fake.names()
    expect(after).toEqual(['resize', 'easeTo'])

    view.rerender(harness({ props: { ...props, loading: true } }))
    view.rerender(harness({ props: { ...props, selectedSegmentId: 'seg-1', onMapClick: vi.fn() } }))
    // 同一个键、同一个锚点，但对象身份都是新的。
    view.rerender(harness({ props: { selectedAnchor: [...RIVER_ANCHOR], autoPan: { ...RIVER_PAN } } }))
    flushFrames()

    expect(fake.names()).toEqual(after)
  })

  it.each([
    ['closing: the key, the anchor and the sheet all go away', { selectedAnchor: null, autoPan: null }, null],
    ['leaving mobile form: the key goes away, the anchor and the window stay', { selectedAnchor: RIVER_ANCHOR, autoPan: null }, 'river'],
  ] as const)('(s3) %s -> no camera method is called', (_name, next, sheet) => {
    const fake = installFakeMap()
    const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    flushFrames()
    const after = fake.names()
    expect(after).toEqual(['resize', 'easeTo'])

    view.rerender(harness({ props: { ...next }, sheet }))
    flushFrames()

    expect(fake.names()).toEqual(after)
  })

  it('(s4) the key changing to another non-empty value eases once more, to the new anchor / the new offset', () => {
    const fake = installFakeMap()
    const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    flushFrames()
    expect(fake.eases()).toHaveLength(1)

    // 布局切换：同一个锚点，遮盖侧换成右侧，抽屉变成 375×294。
    const sideSheet = { width: 375, height: 294 }
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN_SIDE }, sheetSize: sideSheet }))
    flushFrames()
    expect(fake.eases()).toHaveLength(2)
    expect(fake.eases()[1]).toMatchObject({ center: RIVER_ANCHOR, offset: [-sideSheet.width / 2, 0] })

    // 替换：气象代站窗换掉河段窗。
    view.rerender(harness({ props: { selectedAnchor: STATION_ANCHOR, autoPan: STATION_PAN }, sheet: 'station' }))
    flushFrames()
    expect(fake.eases()).toHaveLength(3)
    expect(fake.eases()[2]).toMatchObject({ center: STATION_ANCHOR, offset: [0, -SHEET_SIZE.height / 2] })
    for (const options of fake.eases()) expect(Object.keys(options).sort()).toEqual(['center', 'duration', 'offset'])
  })

  it('(s4) two key changes inside one frame ease once, to the last key', () => {
    const fake = installFakeMap()
    const view = render(harness())
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    view.rerender(harness({ props: { selectedAnchor: STATION_ANCHOR, autoPan: STATION_PAN }, sheet: 'station' }))
    flushFrames()

    expect(fake.names()).toEqual(['resize', 'easeTo'])
    expect(fake.eases()[0]).toMatchObject({ center: STATION_ANCHOR, offset: [0, -SHEET_SIZE.height / 2] })
  })

  it('(s5) a moveend after the user dragged the map does not ease; a later key change eases exactly once', () => {
    const fake = installFakeMap()
    const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    flushFrames()
    expect(fake.eases()).toHaveLength(1)

    fake.camera.shiftY = 180
    fireMoveEnd()
    fireMoveEnd()
    flushFrames()
    expect(fake.names()).toEqual(['resize', 'easeTo'])

    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN_SIDE } }))
    flushFrames()
    expect(fake.eases()).toHaveLength(2)
  })

  it('(s6) under StrictMode one key change still eases exactly once (mounted with an empty key, and mounted with the key already set)', () => {
    const fake = installFakeMap()
    const view = render(<StrictMode>{harness()}</StrictMode>)
    flushFrames()
    expect(fake.names()).toEqual([])
    view.rerender(<StrictMode>{harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } })}</StrictMode>)
    flushFrames()
    expect(fake.names()).toEqual(['resize', 'easeTo'])
    view.unmount()

    // 挂载时键已非空：StrictMode 把挂载 effect 跑两遍，仍只平移一次。
    const remounted = installFakeMap()
    render(<StrictMode>{harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } })}</StrictMode>)
    flushFrames()
    expect(remounted.names()).toEqual(['resize', 'easeTo'])
  })

  it('(s7) a non-empty key with no sheet rendered (or a sheet with no box) does not ease', () => {
    const fake = installFakeMap()
    const view = render(harness({ sheet: null }))
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN }, sheet: null }))
    flushFrames()
    expect(fake.eases()).toHaveLength(0)

    // 渲染着的是另一种窗：不是选中锚点的抽屉。
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN_SIDE }, sheet: 'station' }))
    flushFrames()
    expect(fake.eases()).toHaveLength(0)

    // 节点在、但没有布局盒。
    view.rerender(harness({ props: { selectedAnchor: STATION_ANCHOR, autoPan: STATION_PAN }, sheet: 'station', sheetSize: { width: 0, height: 0 } }))
    flushFrames()
    expect(fake.eases()).toHaveLength(0)
    for (const method of ['flyTo', 'jumpTo', 'panTo', 'fitBounds', 'setCenter', 'setPadding']) expect(fake.names()).not.toContain(method)
  })

  it('(s7) a key that is gone again before the next frame (the panel crashed on its first render) does not ease', () => {
    const fake = installFakeMap()
    const view = render(harness())
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: null } }))
    flushFrames()
    expect(fake.names()).toEqual([])

    // 「重试」成功：同一个键再次出现，平移一次。
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    flushFrames()
    expect(fake.names()).toEqual(['resize', 'easeTo'])
  })

  it('a key that goes away and comes back to the same value inside one frame eases exactly once', () => {
    const fake = installFakeMap()
    const view = render(harness())
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: null } }))
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    flushFrames()

    expect(fake.names()).toEqual(['resize', 'easeTo'])
  })

  it('unmounting with a pan still scheduled cancels that frame', () => {
    installFakeMap()
    const view = render(harness())
    expect(frames).toHaveLength(0)
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    expect(frames).toHaveLength(1)

    view.unmount()

    // 断言取消本身：卸载后地图引用已被置空，残留的帧回调本来就提前返回，“相机零调用”分辨不出有没有取消。
    expect(frames).toHaveLength(0)
  })

  describe('(s8) attributes', () => {
    it('with an anchor both attributes equal the canvas viewport origin + project(), follow project() after moveend, and are absent without an anchor', () => {
      const fake = installFakeMap()
      const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR }, sheet: null }))
      // 桌面形态（没有触发键）也带；值 = 画布视口原点 + project。
      expect(anchorAttributes()).toEqual({ x: String(CANVAS_ORIGIN.left + 50), y: String(CANVAS_ORIGIN.top + 25) })
      expect(fake.names()).toEqual([])

      // 相机动了但还没静止：属性不变；`moveend` 之后按新的 project 更新，至多两位小数。
      fake.camera.shiftX = 12.3456
      fake.camera.shiftY = -7.004
      view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, loading: true }, sheet: null }))
      expect(anchorAttributes()).toEqual({ x: '60', y: '45' })
      fireMoveEnd()
      expect(anchorAttributes()).toEqual({ x: '72.35', y: '38' })

      // 选中锚点换了：不等 `moveend`，立即更新。
      view.rerender(harness({ props: { selectedAnchor: STATION_ANCHOR }, sheet: null }))
      expect(anchorAttributes()).toEqual({ x: '172.35', y: '188' })

      view.rerender(harness({ props: { selectedAnchor: null }, sheet: null }))
      expect(anchorAttributes()).toEqual({ x: null, y: null })
      fireMoveEnd()
      expect(anchorAttributes()).toEqual({ x: null, y: null })
    })

    it('the anchor attributes appear once the map has loaded when it was not ready at first', () => {
      installMaplibreStubMap(null)
      const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR }, sheet: null }))
      expect(anchorAttributes()).toEqual({ x: null, y: null })

      // 地图实例就绪（桩 `<Map>` 在下一次渲染把它交给 ref）；锚点没变，属性要等 `onLoad` 才出现。
      installFakeMap()
      view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR }, sheet: null }))
      expect(anchorAttributes()).toEqual({ x: null, y: null })
      act(() => mapEventProps().onLoad?.())
      expect(anchorAttributes()).toEqual({ x: '60', y: '45' })
    })

    it('with the test gate open the camera attributes equal the fake map after moveend; with the gate closed they are absent', () => {
      const fake = installFakeMap()
      const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR }, sheet: null }))
      fireMoveEnd()
      expect(cameraAttributes()).toEqual({ center: null, zoom: null })
      view.unmount()

      gateWindow.__NHMS_E2E_HOOKS__ = true
      render(harness({ sheet: null }))
      fireMoveEnd()
      // 没有选中锚点也带；6 位 / 4 位小数。
      expect(cameraAttributes()).toEqual({ center: '100.123457,30.987654', zoom: '9.8765' })
      expect(anchorAttributes()).toEqual({ x: null, y: null })

      fake.camera.center = { lng: 99.5, lat: 31 }
      fake.camera.zoom = 12
      expect(cameraAttributes()).toEqual({ center: '100.123457,30.987654', zoom: '9.8765' })
      fireMoveEnd()
      expect(cameraAttributes()).toEqual({ center: '99.500000,31.000000', zoom: '12.0000' })
    })

    it('a truthy but non-boolean gate does not open the camera attributes', () => {
      installFakeMap()
      gateWindow.__NHMS_E2E_HOOKS__ = 'true'
      render(harness({ props: { selectedAnchor: RIVER_ANCHOR }, sheet: null }))
      fireMoveEnd()
      expect(cameraAttributes()).toEqual({ center: null, zoom: null })
      expect(anchorAttributes()).toEqual({ x: '60', y: '45' })
    })
  })

  it('(s9) a fake map with only the shared stub\'s methods, an anchor and a non-empty key: nothing throws and there are no anchor attributes', () => {
    // 共享桩的假地图大致就这几样：`getCanvas()` 只返回 `{ style }`，没有 `project` / `easeTo` / `resize`。
    const fitBounds = vi.fn()
    installMaplibreStubMap({ getMap: () => ({ fitBounds, getCanvas: () => ({ style: { cursor: '' } }) }) })
    gateWindow.__NHMS_E2E_HOOKS__ = true

    const view = render(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    expect(() => flushFrames()).not.toThrow()
    expect(() => fireMoveEnd()).not.toThrow()
    expect(() => act(() => mapEventProps().onLoad?.())).not.toThrow()
    view.rerender(harness({ props: { selectedAnchor: STATION_ANCHOR, autoPan: STATION_PAN }, sheet: 'station' }))
    expect(() => flushFrames()).not.toThrow()

    expect(anchorAttributes()).toEqual({ x: null, y: null })
    expect(cameraAttributes()).toEqual({ center: null, zoom: null })
    expect(fitBounds).not.toHaveBeenCalled()
  })

  it('(s10) resize() is called before easeTo, and the moveend it fires synchronously neither counts as a trigger nor causes a second ease', () => {
    const fake = installFakeMap()
    const view = render(harness())
    view.rerender(harness({ props: { selectedAnchor: RIVER_ANCHOR, autoPan: RIVER_PAN } }))
    expect(anchorAttributes()).toEqual({ x: '60', y: '45' })
    // 相机在这一帧之前动过（没有别的 `moveend`）：属性若更新，只能来自 `resize()` 重入的那一次。
    fake.camera.shiftX = 5
    flushFrames()
    // 再给重入引起的重渲染一次机会。
    flushFrames()

    expect(fake.names()).toEqual(['resize', 'easeTo'])
    expect(fake.reentrantMoveEnds.count).toBe(1)
    expect(anchorAttributes()).toEqual({ x: '65', y: '45' })
  })
})
