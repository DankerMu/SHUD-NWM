import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { M11DraggableCurveWindow } from '@/components/map/M11DraggableCurveWindow'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'

/**
 * 桌面形态曲线窗最小宽度的尺寸回退（openspec mobile-responsive-display task 4.3，design.md D16；
 * tasks.md 里 #2804 Triage 的用例 (h)）。
 *
 * jsdom 没有布局：窗与父容器的包围盒都是 0，默认摆位用的是组件里的尺寸回退，宽度取自
 * `window.innerWidth`。回退宽不导出，经河段窗默认摆位的 `left` = 0.28 × 宽 − 回退宽 / 2 观测。
 * 不 mock 窗自身的 `getBoundingClientRect`——非零的实测盒会绕过回退。
 */
const WINDOW_TEST_ID = 'curve-window'
const RIVER_ANCHOR_RATIO = 0.28

const originalViewport = { width: window.innerWidth, height: window.innerHeight }
let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

function setViewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: height })
}

function defaultRiverLeft(viewportWidth: number) {
  setViewport(viewportWidth, 900)
  form = installMobileFormMatchMedia(false)
  render(
    <M11DraggableCurveWindow kind="river" testId={WINDOW_TEST_ID} header={<div>标题</div>}>
      <div>曲线</div>
    </M11DraggableCurveWindow>,
  )
  const frame = screen.getByTestId(WINDOW_TEST_ID)
  // 前提：走的是回退路径，且窗已定位（不是首帧的隐藏占位）。
  expect(frame.getBoundingClientRect().width).toBe(0)
  expect(frame.style.visibility).toBe('')
  return Number.parseFloat(frame.style.left)
}

afterEach(() => {
  form?.restore()
  form = undefined
  setViewport(originalViewport.width, originalViewport.height)
})

describe('M11DraggableCurveWindow 的桌面尺寸回退宽度', () => {
  it.each([
    { viewportWidth: 1280, fallbackWidth: 537.6, left: 89.6, branch: '42vw 一支，与改动前相同' },
    { viewportWidth: 1000, fallbackWidth: 480, left: 40, branch: '30rem 下限一支（改动前 420 -> left 70）' },
    { viewportWidth: 1920, fallbackWidth: 704, left: 185.6, branch: '44rem 封顶一支，与改动前相同' },
  ])('容器宽 $viewportWidth：回退宽 $fallbackWidth，河段窗默认 left $left（$branch）', ({ viewportWidth, fallbackWidth, left }) => {
    expect(RIVER_ANCHOR_RATIO * viewportWidth - fallbackWidth / 2).toBeCloseTo(left)

    expect(defaultRiverLeft(viewportWidth)).toBeCloseTo(left)
  })
})
