import { expect, test } from '@playwright/test'

import { controlBarParts, expectControlsInViewport, measureControlBar, overlapsVertically } from './support/controlBar.mocked'
import { intersects } from './support/legendLauncher.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { isMobileForm, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 桌面形态的控制条不变（openspec mobile-responsive-display task 3.7，Risk pack「Legacy compatibility」）：
 * 64px 条高、单行、播放速度选择器仍在时间轴里、宽 `min(64rem, 100% − 2rem)` 且水平居中、
 * 控件尺寸与字号仍是桌面值（移动形态的尺寸类没有漏到桌面）。
 */
const TOLERANCE = 0.5
const BAR_HEIGHT = 64
const BAR_MAX_WIDTH = 1024
const BAR_SIDE_INSET = 16
const BAR_BOTTOM_INSET = 40
/** 桌面的 `h-8` / `w-8`。 */
const DESKTOP_CONTROL_SIZE = 32
/**
 * 桌面两个选择器的计算字号（改动前的实测事实）：`src/index.css` 的无层规则 `select { font: inherit }`
 * 压过 Tailwind 工具层，`<select>` 自身的 `text-xs` 不生效，字号取自各自的 `<label>`——
 * 起报时次的 label 没有字号类（继承 16px），播放速度的 label 是 `text-xs`（12px）。
 */
const DESKTOP_CYCLE_FONT_PX = 16
const DESKTOP_SPEED_FONT_PX = 12

test.describe('M11 控制条桌面形态', () => {
  for (const viewport of [
    { width: 1280, height: 900 },
    // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态。
    { width: 768, height: 1024 },
  ]) {
    test(`条高 64、单行、速度选择器在时间轴内、宽度与居中不变 @ ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport)
      expect(isMobileForm(requireViewport(page))).toBe(false)
      await installRiverWindowMocks(page)
      await page.goto('/')
      await expectMapControlsMounted(page)
      const parts = controlBarParts(page)
      await expect(parts.stepButtons[2]).toBeEnabled()

      const where = `${viewport.width}x${viewport.height}`
      const m = await measureControlBar(page, requireViewport(page))
      console.log(`control-bar desktop @ ${where}`, JSON.stringify(m))
      expectControlsInViewport(m, where)

      expect(Math.abs(m.bar.height - BAR_HEIGHT), `条高 ${m.bar.height}`).toBeLessThanOrEqual(TOLERANCE)
      // 单行：滑块、三个步进 / 播放按钮、两个选择器都与预报源分段纵向重叠。
      for (const [name, box] of [
        ['滑块', m.slider],
        ['起报时次', m.cycle],
        ['播放速度', m.speed],
        ...m.stepButtons.map((box, index) => [`步进 / 播放按钮 ${index + 1}`, box] as const),
      ] as const) {
        expect(overlapsVertically(box, m.sourceGroup), `${name}应与预报源分段同在一行`).toBe(true)
      }
      expect(m.speedCount).toBe(1)
      expect(m.speedInsideTimeline, '桌面形态下播放速度选择器应在 m11-timeline 里').toBe(true)

      // 宽 = min(64rem, 地图区宽 − 2rem)，水平居中，底边距地图区底 40px，不压 attribution。
      const expectedWidth = Math.min(BAR_MAX_WIDTH, m.map.width - 2 * BAR_SIDE_INSET)
      expect(Math.abs(m.bar.width - expectedWidth), `条宽 ${m.bar.width}，期望 ${expectedWidth}`).toBeLessThanOrEqual(TOLERANCE)
      const barCenter = m.bar.x + m.bar.width / 2
      const mapCenter = m.map.x + m.map.width / 2
      expect(Math.abs(barCenter - mapCenter), `条中心 ${barCenter}，地图区中心 ${mapCenter}`).toBeLessThanOrEqual(TOLERANCE)
      const bottomInset = m.map.y + m.map.height - (m.bar.y + m.bar.height)
      expect(Math.abs(bottomInset - BAR_BOTTOM_INSET), `条底边距地图区底 ${bottomInset}px`).toBeLessThanOrEqual(TOLERANCE)
      expect(intersects(m.bar, m.attribution), '控制条与版权归属相交').toBe(false)

      // 控件仍是桌面尺寸：移动形态的 44px / 16px 没有漏过来。
      for (const [index, button] of [...m.sourceButtons, ...m.stepButtons].entries()) {
        expect(Math.abs(button.height - DESKTOP_CONTROL_SIZE), `按钮 ${index + 1} 高 ${button.height}`).toBeLessThanOrEqual(TOLERANCE)
      }
      for (const button of m.stepButtons) {
        expect(Math.abs(button.width - DESKTOP_CONTROL_SIZE), `步进 / 播放按钮宽 ${button.width}`).toBeLessThanOrEqual(TOLERANCE)
      }
      for (const [name, box, fontSize, expectedFontSize] of [
        ['起报时次', m.cycle, m.cycleFontSizePx, DESKTOP_CYCLE_FONT_PX],
        ['播放速度', m.speed, m.speedFontSizePx, DESKTOP_SPEED_FONT_PX],
      ] as const) {
        expect(Math.abs(box.height - DESKTOP_CONTROL_SIZE), `${name}高 ${box.height}`).toBeLessThanOrEqual(TOLERANCE)
        expect(fontSize, `${name}计算字号`).toBe(expectedFontSize)
      }
      await expect(parts.reason).toHaveCount(0)
    })
  }
})
