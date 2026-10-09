import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject, type ReactNode } from 'react'
import type { MapRef, MapStyle } from 'react-map-gl/maplibre'

import { buildApiTileUrlTemplate } from '@/api/base'

import type { M11Bbox } from '@/lib/m11/overviewDataContracts'
import type { M11Basemap } from '@/lib/m11/queryState'

export interface M11MapCameraFit {
  bounds: [[number, number], [number, number]]
  padding?: number
}

export interface M11MapCameraFlyTo {
  center: [number, number]
  zoom?: number
}

/** 流域 bbox → 相机 fit（留 36px 内边距）；bbox 缺失时不 fit。 */
export function bboxToMapFit(bbox: M11Bbox | null | undefined): M11MapCameraFit | null {
  if (!bbox) return null
  return {
    bounds: [
      [bbox.minLon, bbox.minLat],
      [bbox.maxLon, bbox.maxLat],
    ],
    padding: 36,
  }
}

type M11InitialViewState = {
  bounds: M11MapCameraFit['bounds']
  fitBoundsOptions: { padding: number }
}

// 全国初始视野按中国范围 fit，而非固定 zoom：固定 zoom 下可见范围随视口 CSS 像素变化
// （大屏外接显示器能看到非洲到太平洋，笔记本只到中国周边）；fit 让任何视口初始都正好框住中国。
export const CHINA_BOUNDS: M11MapCameraFit['bounds'] = [
  [73, 17],
  [135, 54],
]

export const CHINA_VIEW_STATE: M11InitialViewState = {
  bounds: CHINA_BOUNDS,
  fitBoundsOptions: { padding: 32 },
}

// 天地图 `_w` 瓦片从 l=1 起发布；不允许缩到 0 级，也就不会请求不存在的 z0 瓦片。
export const M11_MAP_MIN_ZOOM = 1

export const m11MapStyleUrls: Record<M11Basemap, string> = {
  terrain: 'm11://basemaps/terrain',
  satellite: 'm11://basemaps/satellite',
  vector: 'm11://basemaps/vector',
}

// 天地图（Tianditu）栅格底图，经同源代理 `/api/v1/basemap/tianditu/*` 取瓦片：key 只在服务端
// （NHMS_TIANDITU_KEY），瓦片在服务端文件缓存，访问量不再逐人消耗 key 配额（天地图限流即 429）。
// 每种底图叠「底图 + 中文注记」两层；最底下垫一层纯色 background，瓦片失败时地图不会是空洞。
const TIANDITU_ATTRIBUTION = '© 天地图'
const M11_BASEMAP_BACKGROUND_COLOR = '#eef1f4'
export const M11_BASEMAP_UNAVAILABLE_NOTICE =
  '底图服务暂时不可用（天地图限流或网络异常），河网与预报图层不受影响，底图稍后自动恢复。'

export const m11MapStyles: Record<M11Basemap, MapStyle> = {
  terrain: tiandituStyle('ter', 'cta', 14),
  satellite: tiandituStyle('img', 'cia', 18),
  vector: tiandituStyle('vec', 'cva', 18),
}

const m11BasemapSourceIds = new Set(Object.values(m11MapStyles).flatMap((style) => Object.keys(style.sources)))

export function useM11MapCamera({
  fitTo,
  flyTo,
  mapRef,
}: {
  fitTo?: M11MapCameraFit | null
  flyTo?: M11MapCameraFlyTo | null
  mapRef: MutableRefObject<MapRef | null>
}): M11InitialViewState {
  const lastFitKeyRef = useRef<string | null>(null)
  const lastFlyKeyRef = useRef<string | null>(null)
  // 地图子树 remount 时相机会重置回 initialViewState。首挂载时若已知 fitTo（静态 bbox 已缓存
  // 同步可得），直接用该 bounds 初始化，避免「先闪回全国再飞入」的强制回初始视角（#1）；
  // mount 后到达的 fitTo 仍由下方 effect 兜底。
  const [initialViewState] = useState<M11InitialViewState>(() =>
    fitTo ? { bounds: fitTo.bounds, fitBoundsOptions: { padding: fitTo.padding ?? 32 } } : CHINA_VIEW_STATE,
  )

  useEffect(() => {
    const map = mapRef.current
    if (!fitTo || !map) return
    const fitKey = mapFitKey(fitTo)
    if (fitKey === lastFitKeyRef.current) return
    lastFitKeyRef.current = fitKey
    map.fitBounds(fitTo.bounds, { padding: fitTo.padding ?? 32, duration: 450 })
  }, [fitTo, mapRef])

  useEffect(() => {
    const map = mapRef.current
    if (!flyTo || !map) return
    const flyKey = mapFlyKey(flyTo)
    if (flyKey === lastFlyKeyRef.current) return
    lastFlyKeyRef.current = flyKey
    map.flyTo({ center: flyTo.center, zoom: flyTo.zoom, duration: 450 })
  }, [flyTo, mapRef])

  return initialViewState
}

/** 抽屉遮住地图区的哪一侧：非矮视口横屏为底部抽屉，矮视口横屏为右侧抽屉。 */
export type M11SheetSide = 'bottom' | 'right'

/**
 * 抽屉自动平移的触发（openspec mobile-responsive-display D17）：由页面按**收敛后的状态**与页面那一份形态值算出。
 * `key` 变成另一个值时平移一次；null = 不平移（桌面形态、没有选中锚点、曲线区域在兜底）。
 */
export interface M11SheetAutoPan {
  key: string
  side: M11SheetSide
  /** 选中锚点所属的曲线窗种类：据它在 DOM 里找到真实渲染出来的抽屉。 */
  kind: 'river' | 'station'
}

const M11_CAMERA_DATA_ATTRIBUTES = [
  'data-selected-anchor-x',
  'data-selected-anchor-y',
  'data-camera-center',
  'data-camera-zoom',
] as const
type M11CameraDataAttributes = Partial<Record<(typeof M11_CAMERA_DATA_ATTRIBUTES)[number], string>>

/**
 * 假地图（vitest 的共享桩）只有少数几个方法，所以这里用到的实例方法全部可选，调用前逐个判断存在性；
 * 缺了就跳过那一步，不报错。
 */
interface M11AnchorCameraMap {
  resize?: () => unknown
  easeTo?: (options: { center: [number, number]; offset: [number, number]; duration: number }) => unknown
  project?: (lngLat: [number, number]) => { x: number; y: number }
  getCenter?: () => { lng: number; lat: number }
  getZoom?: () => number
  getCanvas?: () => { getBoundingClientRect?: () => { left: number; top: number } } | null | undefined
}

/** 与流域 `fitBounds` / 聚合 `flyTo` 同一个动画时长。 */
const M11_SHEET_PAN_DURATION_MS = 450

function nativeAnchorCameraMap(mapRef: MutableRefObject<MapRef | null>): M11AnchorCameraMap | null {
  return (mapRef.current?.getMap?.() as M11AnchorCameraMap | null | undefined) ?? null
}

/** 至多两位小数的 CSS px。 */
function cssPx(value: number) {
  return String(Math.round(value * 100) / 100)
}

/** 地图实例未就绪、缺方法、没有选中锚点时，对应的属性就不在返回值里。 */
function readM11CameraDataAttributes(map: M11AnchorCameraMap | null, anchor: [number, number] | null): M11CameraDataAttributes {
  const attributes: M11CameraDataAttributes = {}
  if (!map) return attributes
  const canvasRect = typeof map.getCanvas === 'function' ? map.getCanvas()?.getBoundingClientRect?.() : undefined
  if (anchor && canvasRect && typeof map.project === 'function') {
    const point = map.project(anchor)
    const x = canvasRect.left + point.x
    const y = canvasRect.top + point.y
    if (Number.isFinite(x) && Number.isFinite(y)) {
      attributes['data-selected-anchor-x'] = cssPx(x)
      attributes['data-selected-anchor-y'] = cssPx(y)
    }
  }
  if (
    (window as { __NHMS_E2E_HOOKS__?: unknown }).__NHMS_E2E_HOOKS__ === true &&
    typeof map.getCenter === 'function' &&
    typeof map.getZoom === 'function'
  ) {
    const center = map.getCenter()
    attributes['data-camera-center'] = `${center.lng.toFixed(6)},${center.lat.toFixed(6)}`
    attributes['data-camera-zoom'] = map.getZoom().toFixed(4)
  }
  return attributes
}

/**
 * 选中锚点与相机的只读观测属性 + 抽屉打开时的一次性平移（D17）。
 *
 * - 平移：`autoPan.key` 变成另一个非空值时恰一次 `easeTo`（只给 center / offset / duration，不动缩放、方位、
 *   俯仰，也不用相机内边距）；键不变的重渲染、键变空都不碰相机。没有任何“跟随”状态，所以用户手动移动地图后
 *   不会被拉回。平移时刻地图区里必须真的渲染着抽屉（DOM 实测），尺寸也只取实测值。
 *   平移放在键变化后的下一帧：形态切换时页面与抽屉各有一份 `useMobileForm()` 订阅，`matchMedia` 的 change
 *   逐个监听器派发、React 逐个同步提交，页面那一份先翻——键变化的那次提交里抽屉还是旧形态（实测：
 *   390×664 -> 750×342 量到的是全宽的底部抽屉，1280×900 -> 390×664 量到的是桌面浮窗）。同一帧的
 *   `requestAnimationFrame` 排在全部 change 监听器之后，那时抽屉已是新形态。
 * - 属性：`data-selected-anchor-x / -y` 是选中锚点在当前相机下的视口 CSS px（画布视口原点 + `project`），
 *   只在三个时刻重读：选中锚点变化（绘制前）、相机静止（`moveend`，含画布尺寸变化引起的）、地图加载完成
 *   ——后两个经 `onCameraSettled`，由调用方接到 `<Map>` 的 `onMoveEnd` / `onLoad`。不在渲染期现读：
 *   页面重渲染会把动画中途的值写出来。`data-camera-center` / `data-camera-zoom` 只在测试门打开时输出，
 *   产品逻辑不读它们。
 */
export function useM11SelectedAnchorCamera({
  mapRef,
  surfaceRef,
  selectedAnchor,
  autoPan,
}: {
  mapRef: MutableRefObject<MapRef | null>
  surfaceRef: MutableRefObject<HTMLElement | null>
  selectedAnchor: [number, number] | null
  autoPan: M11SheetAutoPan | null
}): { cameraDataAttributes: M11CameraDataAttributes; onCameraSettled: () => void } {
  // 本次渲染的值，供下面的回调与 effect 读取（它们的依赖只列键 / 经纬度这类原始值）。
  const latestRef = useRef({ autoPan, selectedAnchor })
  latestRef.current = { autoPan, selectedAnchor }

  const [cameraDataAttributes, setCameraDataAttributes] = useState<M11CameraDataAttributes>({})
  const onCameraSettled = useCallback(() => {
    const next = readM11CameraDataAttributes(nativeAnchorCameraMap(mapRef), latestRef.current.selectedAnchor)
    // 值没变就保留原对象：不为一次没有改变任何属性的 `moveend` 重渲染。
    setCameraDataAttributes((current) => (M11_CAMERA_DATA_ATTRIBUTES.every((name) => current[name] === next[name]) ? current : next))
  }, [mapRef])
  // 选中锚点变化（含变空）：绘制前就重读，不留一帧上一个锚点的坐标。
  const anchorLng = selectedAnchor?.[0] ?? null
  const anchorLat = selectedAnchor?.[1] ?? null
  useLayoutEffect(onCameraSettled, [anchorLat, anchorLng, onCameraSettled])

  const panKey = autoPan?.key ?? ''
  const lastPanKeyRef = useRef('')
  useEffect(() => {
    // 先记键再排平移：一次键变化至多尝试一次（StrictMode 的 effect 重放、平移前 `resize()`
    // 同步发出的 `moveend` 引起的重渲染都落在“键没变”上）。
    if (panKey === lastPanKeyRef.current) return
    lastPanKeyRef.current = panKey
    if (!panKey) return
    requestAnimationFrame(() => {
      // 这一帧之内键又变了（含变空）：这次不平移，新键自己会排一次。
      if (lastPanKeyRef.current !== panKey) return
      // 锚点 / 遮盖侧 / 窗种类都已编码在键里：键没变，这里读到的就是排这次平移时的那一份。
      const { autoPan: pan, selectedAnchor: anchor } = latestRef.current
      const map = nativeAnchorCameraMap(mapRef)
      if (!pan || !anchor || !map || typeof map.easeTo !== 'function') return
      // 抽屉是地图区里的兄弟节点（`M11DraggableCurveWindow` 的 frame）；没有渲染出来就不平移——
      // 曲线面板渲染即崩溃而键仍非空时，挡住平移的只有这道闸。
      const sheet = surfaceRef.current?.ownerDocument.querySelector(`[data-m11-curve-window-kind="${pan.kind}"]`)
      if (!sheet) return
      // 形态切换时画布也在变尺寸，而地图库自己的 resize 是异步且节流的；`easeTo` 在起点就把目标屏幕点定为
      // “当时的画布中心 + 偏移”，所以先同步 resize（空闲时幂等）再量抽屉、再平移。
      if (typeof map.resize === 'function') map.resize()
      const { width, height } = sheet.getBoundingClientRect()
      if (!(width > 0 && height > 0)) return
      // 锚点落在未被抽屉遮住的地图区的中心：底部抽屉上移半个抽屉高，右侧抽屉左移半个抽屉宽。
      map.easeTo({
        center: anchor,
        offset: pan.side === 'right' ? [-width / 2, 0] : [0, -height / 2],
        duration: M11_SHEET_PAN_DURATION_MS,
      })
    })
  }, [mapRef, panKey, surfaceRef])

  return { cameraDataAttributes, onCameraSettled }
}

export function m11MapSourceErrorResetKey({
  basinFeatureCount,
  overlaySourceId,
  basemap,
  layer,
  validTime,
}: {
  basinFeatureCount: number
  overlaySourceId?: string | null
  basemap: M11Basemap
  layer: string
  validTime: string | null
}) {
  return [basinFeatureCount, overlaySourceId ?? '', basemap, layer, validTime ?? ''].join('|')
}

export function useM11MapSourceError(resetKey: string) {
  const [mapSourceError, setMapSourceError] = useState<string | null>(null)

  useEffect(() => {
    setMapSourceError(null)
  }, [resetKey])

  const handleMapError = useCallback((event: { error?: { message?: string }; sourceId?: string }) => {
    // 底图瓦片失败每张一个 error 事件：收敛为一条固定提示（同值 setState 不触发重渲染），
    // 且不覆盖已显示的业务图层错误。
    if (event.sourceId && m11BasemapSourceIds.has(event.sourceId)) {
      setMapSourceError((current) => (current && current !== M11_BASEMAP_UNAVAILABLE_NOTICE ? current : M11_BASEMAP_UNAVAILABLE_NOTICE))
      return
    }
    const message = event.error?.message ?? ''
    // 天地图栅格 style 无 glyphs：symbol 文本层（如代站 cluster 计数）的 style 校验错误
    // 只影响该层文字渲染，不影响其它图层——降级为 console 警告，不弹错误横幅。
    if (message.includes('glyphs')) {
      console.warn('[m11-map] symbol text layer skipped (no glyphs in raster basemap style):', message)
      return
    }
    setMapSourceError(message || '地图源加载失败，受影响图层暂不可用。')
  }, [])

  return { mapSourceError, handleMapError }
}

export function M11MapStatusOverlays({
  loading,
  boundaryLoading,
  basinBoundaryOverlayEnabled,
  basinCount,
  basinFeatureCount,
  skippedBasinGeometryCount,
  unavailableReason,
  selectedSegmentMapState,
  selectedSegmentUnavailableReason,
  mapSourceError,
}: {
  loading: boolean
  boundaryLoading: boolean
  basinBoundaryOverlayEnabled: boolean
  basinCount: number
  basinFeatureCount: number
  skippedBasinGeometryCount: number
  unavailableReason: string | null
  selectedSegmentMapState: 'idle' | 'selected-layer' | 'unavailable'
  selectedSegmentUnavailableReason: string | null
  mapSourceError: string | null
}) {
  return (
    // 桌面形态 `contents`：容器不产生盒子，四条状态条仍各按自己的 absolute 定位。
    // 移动形态（mobile-responsive-display design.md D8）：四条状态条是这个纵向 flex 容器的在流子项，
    // 自上而下排在地图区顶部条带里——
    //   top 58px + 上内边距 8px：浮动提示（M11FloatingNotice，top 8px、两行时高 50px）占的第一槽之下，
    //     恒定预留、不随提示是否出现而变；
    //   right 52px + 右内边距 8px：让出启动器列（44px 列宽 + 8px 右边距）和 8px 间隙；
    //   bottom：控制条顶边之上，随控制条的两种移动布局取两个值——
    //     竖屏 240px：竖屏控制条两行、条高由内容决定（task 3.7）。最高的形态是 fail-closed：
    //       第一行 44px + 时间轴行（底行的「Analysis / Forecast」在 320px 宽时折成三行，三个词即上限）
    //       + 单行截断的禁用原因行，实测条高 187px；加底边距 40px = 227px，取整到 15rem 留 13px 余量。
    //     矮视口横屏 104px：单行控制条顶边（底边距 40px + 条高 64px）。task 3.8 改横屏条的几何后须复核。
    //     overflow-hidden 保证放不下的状态条被裁掉而不是伸进控制条（矮视口横屏下两条及以上两行
    //     状态条同时出现时，后面的会被裁）。
    //   四周 8px 内边距同时给状态条的阴影留出不被裁的余地。
    // 容器自身不拦指针（空容器、状态条下方的区域都要能操作地图），状态条本身照常。
    <div
      className="contents mobile:pointer-events-none mobile:absolute mobile:bottom-[15rem] mobile:left-0 mobile:right-[3.25rem] mobile:top-[3.625rem] mobile:z-[90] mobile:flex mobile:flex-col mobile:items-start mobile:gap-2 mobile:overflow-hidden mobile:p-2 mobile:mobile-landscape:bottom-[6.5rem]"
      data-testid="m11-map-status-overlays"
    >
      {basinBoundaryOverlayEnabled && !loading && !boundaryLoading && basinCount > 0 && basinFeatureCount === 0 ? (
        <M11MapStatusNotice testId="m11-basin-layer-unavailable" topClassName="top-20">
          {skippedBasinGeometryCount > 0
            ? '当前可见流域边界超过客户端渲染预算，地图不会注册过大的边界源。'
            : '当前没有可见流域边界。'}
        </M11MapStatusNotice>
      ) : null}

      {!loading && unavailableReason ? (
        <M11MapStatusNotice testId="m11-map-unavailable" topClassName="top-20">
          {unavailableReason}
        </M11MapStatusNotice>
      ) : null}

      {!loading && selectedSegmentMapState === 'unavailable' ? (
        <M11MapStatusNotice testId="m11-selected-segment-map-unavailable" topClassName="top-44">
          {selectedSegmentUnavailableReason}
        </M11MapStatusNotice>
      ) : null}

      {mapSourceError ? (
        <M11MapStatusNotice testId="m11-map-source-error" topClassName="top-32">
          {mapSourceError}
        </M11MapStatusNotice>
      ) : null}
    </div>
  )
}

function M11MapStatusNotice({
  children,
  testId,
  topClassName,
}: {
  children: ReactNode
  testId: string
  topClassName: string
}) {
  return (
    <div
      className={`absolute left-1/2 -translate-x-1/2 ${topClassName} z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md mobile:pointer-events-auto mobile:static mobile:max-w-full mobile:shrink-0 mobile:translate-x-0`}
      role="status"
      data-testid={testId}
    >
      {/* 截断放在内层：直接截带内边距的外层，第三行会从下内边距里露出半行。桌面形态下它只是个普通块。 */}
      <div className="mobile:line-clamp-2">{children}</div>
    </div>
  )
}

// base = 底图图层码（vec/img/ter），annotation = 对应中文注记码（cva/cia/cta）；maxzoom 与天地图
// 各图层发布级别一致（ter/cta 到 14，其余到 18），更高级别由 MapLibre 放大上一级瓦片，不再请求。
function tiandituStyle(base: string, annotation: string, maxzoom: number): MapStyle {
  const source = (layer: string) => ({
    type: 'raster' as const,
    tiles: [buildApiTileUrlTemplate(`/api/v1/basemap/tianditu/${layer}/{z}/{x}/{y}`)],
    tileSize: 256,
    maxzoom,
    attribution: TIANDITU_ATTRIBUTION,
  })
  return {
    version: 8,
    sources: {
      [`${base}-base`]: source(base),
      [`${annotation}-anno`]: source(annotation),
    },
    layers: [
      { id: 'basemap-background', type: 'background', paint: { 'background-color': M11_BASEMAP_BACKGROUND_COLOR } },
      { id: `${base}-base`, type: 'raster', source: `${base}-base` },
      { id: `${annotation}-anno`, type: 'raster', source: `${annotation}-anno` },
    ],
  }
}

function mapFitKey(fitTo: M11MapCameraFit) {
  const [[minLon, minLat], [maxLon, maxLat]] = fitTo.bounds
  return `${minLon},${minLat},${maxLon},${maxLat},${fitTo.padding ?? 32}`
}

function mapFlyKey(flyTo: M11MapCameraFlyTo) {
  return `${flyTo.center[0]},${flyTo.center[1]},${flyTo.zoom ?? ''}`
}
