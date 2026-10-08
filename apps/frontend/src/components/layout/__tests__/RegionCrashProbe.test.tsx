import { render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RegionCrashProbe, type CrashProbeRegion } from '@/components/layout/RegionCrashProbe'

/**
 * 测试门控的区域崩溃探针（openspec mobile-responsive-display task 3.6，design.md D8）：
 * 门控 + 匹配 -> 渲染期抛错；门控 + 不匹配 -> null；无门控 -> null 且**不读取**开关。
 */
const REGIONS: readonly CrashProbeRegion[] = ['map-controls', 'legend', 'control-bar', 'curve']

type SwitchWindow = { __NHMS_E2E_HOOKS__?: unknown; __NHMS_E2E_CRASH_REGION__?: unknown }
const switchWindow = window as unknown as SwitchWindow

/** 把开关装成访问器，返回读取次数的探针。 */
function installSwitchAccessor(value: unknown) {
  const read = vi.fn(() => value)
  Object.defineProperty(window, '__NHMS_E2E_CRASH_REGION__', { configurable: true, get: read })
  return read
}

beforeEach(() => {
  // 无边界时 React 会把渲染期抛错打进日志；这里是预期内的抛错。
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => {
  delete switchWindow.__NHMS_E2E_HOOKS__
  delete switchWindow.__NHMS_E2E_CRASH_REGION__
  vi.restoreAllMocks()
})

describe('RegionCrashProbe', () => {
  it.each(REGIONS)('gate + matching switch throws while rendering (%s)', (region) => {
    switchWindow.__NHMS_E2E_HOOKS__ = true
    switchWindow.__NHMS_E2E_CRASH_REGION__ = region

    expect(() => render(<RegionCrashProbe region={region} />)).toThrow(region)
  })

  it.each(REGIONS)('gate + any other switch value renders nothing (%s)', (region) => {
    switchWindow.__NHMS_E2E_HOOKS__ = true
    const others: unknown[] = [...REGIONS.filter((candidate) => candidate !== region), undefined, '', 'map', true]
    for (const value of others) {
      switchWindow.__NHMS_E2E_CRASH_REGION__ = value
      const { container, unmount } = render(<RegionCrashProbe region={region} />)
      expect(container.innerHTML).toBe('')
      unmount()
    }
  })

  it.each(REGIONS)('no gate: renders nothing and never reads the switch (%s)', (region) => {
    const read = installSwitchAccessor(region)

    const { container } = render(<RegionCrashProbe region={region} />)

    expect(container.innerHTML).toBe('')
    expect(read).not.toHaveBeenCalled()
  })

  it.each([['true'], [1], [{}], [false], [null]])('a gate value that is not exactly `true` (%j) keeps the probe inert', (gate) => {
    switchWindow.__NHMS_E2E_HOOKS__ = gate
    const read = installSwitchAccessor('legend')

    const { container } = render(<RegionCrashProbe region="legend" />)

    expect(container.innerHTML).toBe('')
    expect(read).not.toHaveBeenCalled()
  })

  it('the switch is read at render time: setting it after mount makes the next render throw', () => {
    switchWindow.__NHMS_E2E_HOOKS__ = true
    const { container, rerender } = render(<RegionCrashProbe region="legend" />)
    expect(container.innerHTML).toBe('')

    switchWindow.__NHMS_E2E_CRASH_REGION__ = 'legend'
    expect(() => rerender(<RegionCrashProbe region="legend" />)).toThrow('legend')
  })

  it('never writes either global', () => {
    switchWindow.__NHMS_E2E_HOOKS__ = true
    switchWindow.__NHMS_E2E_CRASH_REGION__ = 'curve'

    render(<RegionCrashProbe region="legend" />)

    expect(switchWindow.__NHMS_E2E_HOOKS__).toBe(true)
    expect(switchWindow.__NHMS_E2E_CRASH_REGION__).toBe('curve')
  })
})
