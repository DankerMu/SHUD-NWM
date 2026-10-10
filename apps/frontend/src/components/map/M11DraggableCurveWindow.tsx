import { useCallback, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'

import { M11_POPUP_GLASS } from '@/components/map/M11PopupChrome'
import { useMobileForm } from '@/hooks/useMobileForm'
import { cn } from '@/lib/cn'

type CurveWindowKind = 'river' | 'station'

const WINDOW_MARGIN_PX = 12
const DESKTOP_PLACEMENT_WIDTH = 900
// 桌面摆位（地图区宽 ≥ 900）：两窗的中心分别锚在地图区宽的这两个比例上。
const RIVER_ANCHOR_RATIO = 0.28
const STATION_ANCHOR_RATIO = 0.72
const DESKTOP_DEFAULT_TOP_PX = 88
/**
 * 两窗默认的横向范围重叠时（窗宽 > 两锚点间距）气象代站窗默认 top 的下移量：它在右，会盖住河段窗右端的
 * 关闭按钮（实测占窗顶 +14 到 +42），下移后该按钮整个露在它之外。不能取得刚好够：桌面形态最矮的视口
 * 900×500（地图区高 416）里窗被夹回到 top 134，实际错开只剩 46px。
 */
const OVERLAP_STAGGER_PX = 56
// 桌面形态默认宽度 min(44rem, max(42vw, 30rem))（design.md D16）有两处表达，必须一致：
// `DESKTOP_WINDOW_CLASS` 的宽度一段，与 `curveWindowSize` 在窗尚无实测盒时的回退。
const MAX_DESKTOP_WIDTH_PX = 704
/** 30rem：桌面形态曲线窗的最小默认宽度。 */
const MIN_DESKTOP_WIDTH_PX = 480
const DESKTOP_WIDTH_RATIO = 0.42
const ASPECT_RATIO_HEIGHT = 9 / 16

// 窗的类串按形态整串选择（design.md D9），不把 `mobile:` 变体叠在桌面类串上：`md:`（宽 ≥ 768）在
// 844×390 这类矮视口横屏同时命中，层叠次序不可靠。两个抽屉的尺寸只由视口与地图区（窗的包含块）决定。
const DESKTOP_WINDOW_CLASS =
  'absolute flex aspect-video w-[calc(100%_-_1.5rem)] max-h-[calc(100%_-_1.5rem)] flex-col overflow-hidden md:w-[min(44rem,max(42vw,30rem))]'
/** 非矮视口横屏的移动形态：贴地图区底、全宽、高 min(60dvh, 地图区高 − 8px)。 */
const BOTTOM_SHEET_CLASS = 'absolute inset-x-0 bottom-0 flex h-[min(60dvh,calc(100%-8px))] flex-col overflow-hidden'
/** 矮视口横屏：贴地图区右、全高、宽 min(50vw, 28rem)。 */
const SIDE_SHEET_CLASS = 'absolute inset-y-0 right-0 flex w-[min(50vw,28rem)] flex-col overflow-hidden'

interface CurveWindowViewport {
  width: number
  height: number
}

interface CurveWindowSize {
  width: number
  height: number
}

interface CurveWindowPosition {
  x: number
  y: number
}

interface ActiveDrag {
  pointerId: number | null
  offsetX: number
  offsetY: number
  ownerWindow: Window
  /** 这次拖拽注册在 `ownerWindow` 上的两个回调：结束拖拽时按同一引用移除。 */
  onMove: (event: PointerEvent) => void
  onStop: (event: PointerEvent) => void
}

export function M11DraggableCurveWindow({
  kind,
  active = true,
  onActivate,
  testId,
  header,
  children,
  className,
}: {
  kind: CurveWindowKind
  active?: boolean
  onActivate?: () => void
  testId: string
  header: ReactNode
  children: ReactNode
  className?: string
}) {
  // 移动形态（design.md D9）：窗是贴边抽屉，位置完全由类决定——不写内联定位、不可拖。
  const { mobile, landscape } = useMobileForm()
  const frameRef = useRef<HTMLElement | null>(null)
  const positionRef = useRef<CurveWindowPosition | null>(null)
  const dragRef = useRef<ActiveDrag | null>(null)
  const [position, setPositionState] = useState<CurveWindowPosition | null>(null)
  const [dragging, setDragging] = useState(false)

  const setPosition = useCallback((next: CurveWindowPosition) => {
    positionRef.current = next
    setPositionState(next)
  }, [])

  const clampCurrentPosition = useCallback(() => {
    const frame = frameRef.current
    if (!frame) return
    const next = clampPosition(frame, positionRef.current ?? defaultPosition(frame, kind))
    setPosition(next)
  }, [kind, setPosition])

  const handleMove = useCallback((event: PointerEvent) => {
    const frame = frameRef.current
    const drag = dragRef.current
    if (!frame || !drag) return
    const pointerId = getPointerId(event)
    if (drag.pointerId !== null && pointerId !== null && drag.pointerId !== pointerId) return
    setPosition(clampPosition(frame, { x: event.clientX - drag.offsetX, y: event.clientY - drag.offsetY }))
  }, [setPosition])

  /** 结束进行中的拖拽：移除 window 监听、释放 pointer capture、`dragging` 归零。没有拖拽时是空操作。 */
  const endDrag = useCallback(() => {
    const drag = dragRef.current
    if (!drag) return
    drag.ownerWindow.removeEventListener('pointermove', drag.onMove)
    drag.ownerWindow.removeEventListener('pointerup', drag.onStop)
    drag.ownerWindow.removeEventListener('pointercancel', drag.onStop)
    if (drag.pointerId !== null) {
      try {
        frameRef.current?.releasePointerCapture(drag.pointerId)
      } catch {
        // Pointer capture may be absent in jsdom or already released by the browser.
      }
    }
    dragRef.current = null
    setDragging(false)
  }, [])

  const stopDrag = useCallback((event: PointerEvent) => {
    const drag = dragRef.current
    if (!drag) return
    const pointerId = getPointerId(event)
    if (drag.pointerId !== null && pointerId !== null && drag.pointerId !== pointerId) return
    endDrag()
  }, [endDrag])

  // 桌面形态：挂载时与从移动形态回来时都取默认位置（不恢复进入移动形态前的拖拽坐标）。
  // 移动形态：清掉位置、终止进行中的拖拽——抽屉的位置只由类决定。
  useLayoutEffect(() => {
    if (mobile) {
      endDrag()
      positionRef.current = null
      setPositionState(null)
      return
    }
    const frame = frameRef.current
    if (!frame) return
    setPosition(clampPosition(frame, defaultPosition(frame, kind)))
  }, [endDrag, kind, mobile, setPosition])

  // 卸载时终止进行中的拖拽：window 上的三个监听不能留到窗没了之后。用 layout effect——清理时 frame 还在，
  // pointer capture 也一并释放。
  useLayoutEffect(() => endDrag, [endDrag])

  // 视口变化时把窗夹回地图区内；移动形态不注册（没有可夹的位置）。
  useLayoutEffect(() => {
    if (mobile) return
    const ownerWindow = frameRef.current?.ownerDocument.defaultView ?? window
    ownerWindow.addEventListener('resize', clampCurrentPosition)
    return () => ownerWindow.removeEventListener('resize', clampCurrentPosition)
  }, [clampCurrentPosition, mobile])

  const startDrag = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      onActivate?.()
      if (event.button !== 0 || shouldIgnoreDragTarget(event.target)) return
      const frame = frameRef.current
      if (!frame) return
      const current = positionRef.current ?? clampPosition(frame, defaultPosition(frame, kind))
      setPosition(current)
      const ownerWindow = frame.ownerDocument.defaultView ?? window
      const pointerId = getPointerId(event.nativeEvent)
      dragRef.current = {
        pointerId,
        offsetX: event.clientX - current.x,
        offsetY: event.clientY - current.y,
        ownerWindow,
        onMove: handleMove,
        onStop: stopDrag,
      }
      if (pointerId !== null) {
        try {
          frame.setPointerCapture(pointerId)
        } catch {
          // Pointer capture is progressive enhancement for this interaction.
        }
      }
      ownerWindow.addEventListener('pointermove', handleMove)
      ownerWindow.addEventListener('pointerup', stopDrag)
      ownerWindow.addEventListener('pointercancel', stopDrag)
      setDragging(true)
      event.preventDefault()
    },
    [handleMove, kind, onActivate, setPosition, stopDrag],
  )

  // 层级两种形态都保留：已高于控制条（115）与启动器列（120）。
  const zIndex = active ? 142 : 132
  const inlineStyle = mobile
    ? { zIndex }
    : position
      ? {
          left: `${position.x}px`,
          top: `${position.y}px`,
          zIndex,
        }
      : {
          left: 0,
          top: 0,
          visibility: 'hidden' as const,
          zIndex,
        }

  return (
    <aside
      ref={frameRef}
      className={cn(
        mobile ? (landscape ? SIDE_SHEET_CLASS : BOTTOM_SHEET_CLASS) : DESKTOP_WINDOW_CLASS,
        M11_POPUP_GLASS,
        // 底部抽屉的圆角只在上方；须排在玻璃类之后（它带四角圆角）。
        mobile && !landscape && 'rounded-b-none',
        className,
      )}
      style={inlineStyle}
      data-testid={testId}
      data-m11-curve-window-kind={kind}
      data-m11-curve-window-active={active ? 'true' : 'false'}
      onPointerDownCapture={onActivate}
      onFocusCapture={onActivate}
      onClickCapture={onActivate}
    >
      <div className="h-px shrink-0 bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" aria-hidden="true" />
      <div
        className={mobile ? 'shrink-0 select-none' : cn('shrink-0 touch-none select-none cursor-grab', dragging && 'cursor-grabbing')}
        data-testid={`${testId}-drag-handle`}
        onPointerDown={mobile ? undefined : startDrag}
      >
        {header}
      </div>
      {/*
        主体容器两种形态都渲染（形态切换不重挂子树）：桌面为 `contents`，不产生布局盒；
        移动形态自己纵向滚动，头部留在它之外、不随主体滚动。
      */}
      <div
        className={mobile ? 'flex min-h-0 flex-1 flex-col overflow-y-auto' : 'contents'}
        data-testid={`${testId}-body`}
      >
        {children}
      </div>
    </aside>
  )
}

function shouldIgnoreDragTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) return false
  return Boolean(
    target.closest(
      [
        '[data-m11-window-no-drag]',
        'button',
        'a',
        'input',
        'textarea',
        'select',
        '[role="button"]',
        '[role="combobox"]',
        '[role="listbox"]',
        '[role="option"]',
        '[role="tab"]',
      ].join(','),
    ),
  )
}

function getPointerId(event: PointerEvent | MouseEvent) {
  return 'pointerId' in event && Number.isFinite(event.pointerId) ? event.pointerId : null
}

function defaultPosition(frame: HTMLElement, kind: CurveWindowKind): CurveWindowPosition {
  const viewport = curveWindowViewport(frame)
  const size = curveWindowSize(frame, viewport)
  const desktop = viewport.width >= DESKTOP_PLACEMENT_WIDTH
  if (!desktop) {
    return { x: viewport.width / 2 - size.width / 2 + (kind === 'river' ? -18 : 18), y: kind === 'river' ? 64 : 88 }
  }
  const x = viewport.width * (kind === 'river' ? RIVER_ANCHOR_RATIO : STATION_ANCHOR_RATIO) - size.width / 2
  // 只看窗的 kind 与地图区尺寸，不看另一窗是否打开：重叠区间里单开气象代站窗也在下移后的位置。
  const overlapping = size.width > viewport.width * (STATION_ANCHOR_RATIO - RIVER_ANCHOR_RATIO)
  const y = DESKTOP_DEFAULT_TOP_PX + (overlapping && kind === 'station' ? OVERLAP_STAGGER_PX : 0)
  return { x, y }
}

function clampPosition(frame: HTMLElement, position: CurveWindowPosition): CurveWindowPosition {
  const viewport = curveWindowViewport(frame)
  const size = curveWindowSize(frame, viewport)
  const maxX = Math.max(WINDOW_MARGIN_PX, viewport.width - size.width - WINDOW_MARGIN_PX)
  const maxY = Math.max(WINDOW_MARGIN_PX, viewport.height - size.height - WINDOW_MARGIN_PX)
  return {
    x: clamp(position.x, WINDOW_MARGIN_PX, maxX),
    y: clamp(position.y, WINDOW_MARGIN_PX, maxY),
  }
}

function curveWindowViewport(frame: HTMLElement): CurveWindowViewport {
  const ownerWindow = frame.ownerDocument.defaultView ?? window
  const parentRect = frame.parentElement?.getBoundingClientRect()
  const width = parentRect && parentRect.width > 0 ? parentRect.width : ownerWindow.innerWidth
  const height = parentRect && parentRect.height > 0 ? parentRect.height : ownerWindow.innerHeight
  return {
    width: Math.max(width, WINDOW_MARGIN_PX * 2),
    height: Math.max(height, WINDOW_MARGIN_PX * 2),
  }
}

function curveWindowSize(frame: HTMLElement, viewport: CurveWindowViewport): CurveWindowSize {
  const rect = frame.getBoundingClientRect()
  if (rect.width > 0 && rect.height > 0) return { width: rect.width, height: rect.height }
  const availableWidth = Math.max(WINDOW_MARGIN_PX * 2, viewport.width - WINDOW_MARGIN_PX * 2)
  const width =
    viewport.width >= DESKTOP_PLACEMENT_WIDTH
      ? Math.min(MAX_DESKTOP_WIDTH_PX, Math.max(viewport.width * DESKTOP_WIDTH_RATIO, MIN_DESKTOP_WIDTH_PX))
      : availableWidth
  const height = Math.min(width * ASPECT_RATIO_HEIGHT, Math.max(WINDOW_MARGIN_PX * 2, viewport.height - WINDOW_MARGIN_PX * 2))
  return { width, height }
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max)
}
