import { useCallback, useEffect, useLayoutEffect, useState, type RefObject } from 'react'

/**
 * 移动形态下地图浮层的展开值（openspec mobile-responsive-display design.md D5）。
 * 同一时刻至多一个面板展开；`null` = 全部收起。不进 URL、不触发任何请求。
 */
export type M11OverlayPanel = 'layers' | 'basemap' | 'legend'
export type M11OverlayExpansion = M11OverlayPanel | null

/** 点某个启动器：已展开则收起，否则展开它（同时收起其他）。 */
export function toggleM11OverlayExpansion(current: M11OverlayExpansion, panel: M11OverlayPanel): M11OverlayExpansion {
  return current === panel ? null : panel
}

export interface M11OverlayExpansionControls {
  /** 桌面形态恒为 `null`：桌面渲染路径不读展开值。 */
  expanded: M11OverlayExpansion
  toggle: (panel: M11OverlayPanel) => void
  /** 复位入口：点地图、`Escape`、形态切换都走这里；曲线抽屉打开时的复位（4.x）也接它。 */
  collapse: () => void
}

/**
 * 展开值的唯一持有者。写入点恰四个：`toggle`（启动器）、`collapse`（地图点击）、
 * `Escape`（仅移动形态且有展开值时挂 document 级监听）、形态切换（`mobile` 翻转即复位）。
 */
export function useM11OverlayExpansion(mobile: boolean): M11OverlayExpansionControls {
  const [value, setValue] = useState<M11OverlayExpansion>(null)
  const expanded = mobile ? value : null

  const toggle = useCallback((panel: M11OverlayPanel) => {
    setValue((current) => toggleM11OverlayExpansion(current, panel))
  }, [])
  const collapse = useCallback(() => setValue(null), [])

  // 形态切换复位：离开移动形态再回来时不得带着旧的展开值。
  useEffect(() => {
    setValue(null)
  }, [mobile])

  useEffect(() => {
    if (expanded === null) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setValue(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [expanded])

  return { expanded, toggle, collapse }
}

/** 展开面板与控制条之间、以及没有控制条时与 attribution 带之间留的间隙。 */
const PANEL_GAP_PX = 8
/** 没有控制条可量时让出的底部带：地图区底 40px 是 attribution 带（控制条 `bottom-10` 的同一条规则）。 */
const ATTRIBUTION_BAND_PX = 40

/**
 * 展开面板的可用高度，按实际几何推出：面板顶边对齐启动器列顶边，底边不得越过 `floor`
 * 给出的元素（控制条）顶边；该元素不在时退到地图区底的 attribution 带之上。
 * 不写死头部或控制条的高度——它们在移动形态下会变（头部 48px、竖屏控制条两行）。
 * `active` 为假时不量、不订阅，返回 `null`。
 */
export function useM11MobilePanelMaxHeight({
  active,
  regionRef,
  columnRef,
  floorSelector,
}: {
  active: boolean
  regionRef: RefObject<HTMLElement | null>
  columnRef: RefObject<HTMLElement | null>
  floorSelector: string
}): number | null {
  const [maxHeight, setMaxHeight] = useState<number | null>(null)

  useLayoutEffect(() => {
    if (!active) {
      setMaxHeight(null)
      return
    }
    const region = regionRef.current
    const column = columnRef.current
    if (!region || !column) return
    const floor = region.querySelector<HTMLElement>(floorSelector)
    const measure = () => {
      const bottomLimit = floor ? floor.getBoundingClientRect().top : region.getBoundingClientRect().bottom - ATTRIBUTION_BAND_PX
      const available = Math.floor(bottomLimit - column.getBoundingClientRect().top - PANEL_GAP_PX)
      // 量不出正值（未布局、jsdom）时不设上限，交给 CSS。
      setMaxHeight(available > 0 ? available : null)
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(region)
    if (floor) observer.observe(floor)
    return () => observer.disconnect()
  }, [active, columnRef, floorSelector, regionRef])

  return maxHeight
}
