import { MOBILE_FORM_QUERY, MOBILE_LANDSCAPE_QUERY } from '@/hooks/useMobileForm'

type ChangeListener = (event: MediaQueryListEvent) => void

/**
 * 可控的 `matchMedia` 桩：移动形态与矮视口横屏两条查询受控，其余查询恒不命中。
 * 矮视口横屏缺省不命中，且只有 `setForm` 能改它——只传 `initialMobile`、只用 `setMobile` 的调用方
 * 看到的行为与只控移动形态那条查询时相同。
 * 测试 setup 的全局桩恒为桌面形态；需要移动形态的用例装这个，用后调用 `restore()` 还原。
 */
export function installMobileFormMatchMedia(initialMobile: boolean, initialLandscape = false) {
  const original = window.matchMedia
  let mobile = initialMobile
  let landscape = initialLandscape
  const listeners = new Set<ChangeListener>()

  const notify = () => {
    // `useMobileForm` 的订阅回调会把两条查询都重读一遍，通知一次即可。
    for (const listener of [...listeners]) {
      listener({ matches: mobile, media: MOBILE_FORM_QUERY } as MediaQueryListEvent)
    }
  }

  window.matchMedia = ((query: string) => {
    const controlled = query === MOBILE_FORM_QUERY || query === MOBILE_LANDSCAPE_QUERY
    return {
      get matches() {
        if (query === MOBILE_FORM_QUERY) return mobile
        // 矮视口横屏必然同时命中移动形态。
        return query === MOBILE_LANDSCAPE_QUERY && mobile && landscape
      },
      media: query,
      onchange: null,
      addEventListener: (type: string, listener: ChangeListener) => {
        if (controlled && type === 'change') listeners.add(listener)
      },
      removeEventListener: (type: string, listener: ChangeListener) => {
        if (controlled && type === 'change') listeners.delete(listener)
      },
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
    } as unknown as MediaQueryList
  }) as typeof window.matchMedia

  return {
    /** 翻转形态并通知订阅者；调用方负责包 `act`。 */
    setMobile(next: boolean) {
      mobile = next
      notify()
    },
    /** 同时设定移动形态与矮视口横屏并通知订阅者；调用方负责包 `act`。 */
    setForm(next: { mobile: boolean; landscape: boolean }) {
      mobile = next.mobile
      landscape = next.landscape
      notify()
    },
    restore() {
      window.matchMedia = original
    },
  }
}
