import { MOBILE_FORM_QUERY } from '@/hooks/useMobileForm'

type ChangeListener = (event: MediaQueryListEvent) => void

/**
 * 可控的 `matchMedia` 桩：只有移动形态那条查询受控，其余查询（含矮视口横屏）恒不命中。
 * 测试 setup 的全局桩恒为桌面形态；需要移动形态的用例装这个，用后调用 `restore()` 还原。
 */
export function installMobileFormMatchMedia(initialMobile: boolean) {
  const original = window.matchMedia
  let mobile = initialMobile
  const listeners = new Set<ChangeListener>()

  window.matchMedia = ((query: string) => {
    const controlled = query === MOBILE_FORM_QUERY
    return {
      get matches() {
        return controlled && mobile
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
      for (const listener of [...listeners]) {
        listener({ matches: next, media: MOBILE_FORM_QUERY } as MediaQueryListEvent)
      }
    },
    restore() {
      window.matchMedia = original
    },
  }
}
