import { useEffect, useState } from 'react'

/**
 * 移动形态判据（openspec mobile-responsive-display design.md D1）的 JS 侧单一来源。
 * `src/index.css` 的 `mobile` / `mobile-landscape` 两个 `@custom-variant` 必须逐字使用同两条查询；
 * `src/__tests__/indexCssViewportVariants.test.ts` 把这条约束钉成测试。
 */

/** 移动形态 = 视口宽 < 768px 或高 < 500px。 */
export const MOBILE_FORM_QUERY = '(max-width: 767.98px), (max-height: 499.98px)'
/** 矮视口横屏 = 高 < 500px 且横屏（必然同时命中移动形态）。 */
export const MOBILE_LANDSCAPE_QUERY = '(max-height: 499.98px) and (orientation: landscape)'

export interface MobileForm {
  mobile: boolean
  landscape: boolean
}

/** `matchMedia` 不存在或抛错（旧 jsdom 桩、非浏览器环境）时返回 null，调用方按桌面形态处理。 */
function queryList(query: string): MediaQueryList | null {
  try {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
    return window.matchMedia(query) ?? null
  } catch {
    return null
  }
}

function readForm(): MobileForm {
  return {
    mobile: queryList(MOBILE_FORM_QUERY)?.matches === true,
    landscape: queryList(MOBILE_LANDSCAPE_QUERY)?.matches === true,
  }
}

export function useMobileForm(): MobileForm {
  const [form, setForm] = useState<MobileForm>(readForm)

  useEffect(() => {
    const lists = [queryList(MOBILE_FORM_QUERY), queryList(MOBILE_LANDSCAPE_QUERY)].filter(
      (list): list is MediaQueryList => list !== null && typeof list.addEventListener === 'function',
    )

    const sync = () => {
      const next = readForm()
      setForm((current) =>
        current.mobile === next.mobile && current.landscape === next.landscape ? current : next,
      )
    }

    for (const list of lists) list.addEventListener('change', sync)
    // 首次渲染与订阅之间视口可能已经变过，订阅后补读一次。
    sync()

    return () => {
      for (const list of lists) list.removeEventListener('change', sync)
    }
  }, [])

  return form
}
