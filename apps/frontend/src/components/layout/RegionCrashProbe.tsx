/** 区域崩溃开关 `window.__NHMS_E2E_CRASH_REGION__` 的四个取值，各对应 `/` 的一个区域错误边界。 */
export type CrashProbeRegion = 'map-controls' | 'legend' | 'control-bar' | 'curve'

/**
 * 测试门控的区域崩溃探针（openspec mobile-responsive-display D8）：放在某个 `RegionErrorBoundary`
 * 之内，让测试在真实浏览器里把该区域送进兜底，从而量兜底块的几何。
 *
 * 先判门控：`window.__NHMS_E2E_HOOKS__ !== true` 时直接返回 null，**不读取**开关。
 * 有门控且开关等于本区域标识时在渲染期抛错，否则返回 null。
 * 不渲染任何 DOM、不持有状态、不订阅、不写任何全局——非崩溃态下所在区域的 DOM 与没有探针时相同。
 * 每次渲染都重新读开关，所以兜底的「重试」沿用既有语义：开关仍在则再次进入兜底，清掉后重试即恢复。
 */
export function RegionCrashProbe({ region }: { region: CrashProbeRegion }): null {
  if ((window as { __NHMS_E2E_HOOKS__?: unknown }).__NHMS_E2E_HOOKS__ !== true) return null
  if ((window as { __NHMS_E2E_CRASH_REGION__?: unknown }).__NHMS_E2E_CRASH_REGION__ === region) {
    throw new Error(`[RegionCrashProbe] test-gated crash of region "${region}"`)
  }
  return null
}
