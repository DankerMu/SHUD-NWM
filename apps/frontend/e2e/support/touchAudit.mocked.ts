import { expect, type Page } from '@playwright/test'

import type { Box } from './legendLauncher.mocked'
import { MIN_FORM_FONT_PX, MIN_TOUCH_TARGET_PX } from './sheetControls.mocked'

/**
 * 全页触控与字号审计（openspec mobile-responsive-display task 5.1，design.md D13 / D15）的枚举与量具。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用。
 *
 * 审计是遍历式的：控件集合由页面当前 DOM 按 `TAPPABLE_CONTROL_SELECTOR` 枚举得出，这里没有 testid 清单。
 * 点名（“枚举结果里必须有某个控件”）由 spec 在枚举结果上做，不反过来决定枚举范围。
 */

/** 可点控件的选择器（tasks.md #2812 口径 (1)）；`canvas` 在枚举时按标签排除。 */
export const TAPPABLE_CONTROL_SELECTOR = [
  'button',
  'a[href]',
  'select',
  'input:not([type=hidden])',
  'summary',
  '[role=button]',
  '[role=tab]',
  '[role=combobox]',
  '[role=switch]',
  '[role=checkbox]',
  '[role=radio]',
  '[role=menuitem]',
  '[role=option]',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')
/** 表单选择控件：字号下限的对象。 */
export const FORM_SELECT_CONTROL_SELECTOR = 'select, [role=combobox]'
/** 仅有的两类豁免（口径 (2)）：MapLibre attribution 子树、开发用角色切换器的触发器。 */
export const EXEMPT_SUBTREE_SELECTOR = '.maplibregl-ctrl-attrib'
export const EXEMPT_ROLE_SELECTOR = '[role=combobox][aria-label="Role"]'

const EDGE_TOLERANCE_PX = 0.5

export interface AuditedControl {
  /** 失败信息里用来认出控件的名字：testid、aria-label、关联 label 的文本、自身文本或标签名。 */
  name: string
  tag: string
  testId: string | null
  ariaLabel: string | null
  /** `input` 的 `type`；其余为 null。 */
  inputType: string | null
  ariaExpanded: string | null
  disabled: boolean
  /** 自身及全部祖先上的 `data-testid`（由内到外）：供 spec 判断控件属于哪个面板 / 抽屉 / 控制条。 */
  testIdPath: string[]
  /** 最近的 `[role=group]` 祖先的 aria-label。 */
  groupLabel: string | null
  /** 点击区（口径 (3)）：元素自己的包围盒；`input` 有关联 `label` 时取该 label 的包围盒。 */
  box: Box
  fontSizePx: number
  formSelect: boolean
  /** 处在纵向滚动容器（计算 `overflow-y` 为 auto / scroll 的祖先）内。 */
  inVerticalScroller: boolean
  /** 纵向滚动容器成员的可见纵向区间（点击区与全部裁剪祖先的交集）；完全滚出为 null。非成员恒为 null。 */
  visibleY: { top: number; bottom: number } | null
  /** 横向真的可滚动的祖先（`overflow-x` 为 auto / scroll 且 `scrollWidth > clientWidth + 1`）；正常为 null。 */
  horizontalScroller: string | null
}

export interface TouchAudit {
  viewport: { width: number; height: number }
  controls: AuditedControl[]
  /** 被豁免的可见控件，只为在日志里留痕。 */
  exempt: Array<{ name: string; reason: 'attribution' | 'role-selector'; box: Box }>
}

/** 枚举页面此刻全部可见可点控件并量取；口径见 tasks.md #2812 的 (1)–(4)。 */
export async function auditControls(page: Page): Promise<TouchAudit> {
  return page.evaluate(
    ({ selector, formSelectSelector, exemptSubtree, exemptRole }) => {
      type Rect = { left: number; top: number; right: number; bottom: number }
      const boxOf = (rect: Rect) => ({ x: rect.left, y: rect.top, width: rect.right - rect.left, height: rect.bottom - rect.top })
      const describe = (element: Element) => {
        const testId = element.getAttribute('data-testid')
        return `${element.tagName.toLowerCase()}${testId ? `[data-testid=${testId}]` : ''}`
      }
      const nameOf = (element: Element) => {
        const labels = element instanceof HTMLInputElement || element instanceof HTMLSelectElement ? [...(element.labels ?? [])] : []
        const candidates = [
          element.getAttribute('data-testid'),
          element.getAttribute('aria-label'),
          labels[0]?.textContent,
          element instanceof HTMLSelectElement ? null : element.textContent,
        ]
        const found = candidates.map((value) => (value ?? '').trim().replace(/\s+/g, ' ')).find((value) => value !== '')
        return (found ?? element.tagName.toLowerCase()).slice(0, 40)
      }

      const controls = []
      const exempt = []
      for (const element of document.querySelectorAll<HTMLElement>(selector)) {
        if (element.tagName === 'CANVAS') continue
        const style = getComputedStyle(element)
        const own = element.getBoundingClientRect()
        if (own.width === 0 || own.height === 0) continue
        if (style.display === 'none' || style.visibility !== 'visible') continue
        if (element.closest('[inert], [aria-hidden="true"]')) continue

        const label = element instanceof HTMLInputElement ? (element.labels?.[0] ?? null) : null
        const hit: Rect = label ? label.getBoundingClientRect() : own

        // 逐个祖先（overflow 非 visible 的才裁剪）：与控件无交集的记为“被裁掉”，同时收窄可见纵向区间。
        // 纵向滚动容器的成员整体跳过“被裁掉”的判定（滚出的行仍计入下限），只保留可见纵向区间。
        let clipped = false
        let inVerticalScroller = false
        let visibleTop = hit.top
        let visibleBottom = hit.bottom
        let horizontalScroller: string | null = null
        for (let ancestor = element.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
          const ancestorStyle = getComputedStyle(ancestor)
          const overflowX = ancestorStyle.overflowX
          const overflowY = ancestorStyle.overflowY
          if ((overflowX === 'auto' || overflowX === 'scroll') && ancestor.scrollWidth > ancestor.clientWidth + 1) {
            horizontalScroller ??= describe(ancestor)
          }
          if (overflowX === 'visible' && overflowY === 'visible') continue
          const rect = ancestor.getBoundingClientRect()
          if (overflowY === 'auto' || overflowY === 'scroll') inVerticalScroller = true
          if (own.right <= rect.left || own.left >= rect.right || own.bottom <= rect.top || own.top >= rect.bottom) clipped = true
          visibleTop = Math.max(visibleTop, rect.top)
          visibleBottom = Math.min(visibleBottom, rect.bottom)
        }
        if (clipped && !inVerticalScroller) continue

        if (element.closest(exemptSubtree)) {
          exempt.push({ name: nameOf(element), reason: 'attribution' as const, box: boxOf(own) })
          continue
        }
        if (element.matches(exemptRole)) {
          exempt.push({ name: nameOf(element), reason: 'role-selector' as const, box: boxOf(own) })
          continue
        }

        const testIdPath: string[] = []
        for (let node: Element | null = element; node; node = node.parentElement) {
          const testId = node.getAttribute('data-testid')
          if (testId) testIdPath.push(testId)
        }
        controls.push({
          name: nameOf(element),
          tag: element.tagName.toLowerCase(),
          testId: element.getAttribute('data-testid'),
          ariaLabel: element.getAttribute('aria-label'),
          inputType: element instanceof HTMLInputElement ? element.type : null,
          ariaExpanded: element.getAttribute('aria-expanded'),
          disabled: element.matches(':disabled'),
          testIdPath,
          groupLabel: element.closest('[role=group]')?.getAttribute('aria-label') ?? null,
          box: boxOf(hit),
          fontSizePx: Number.parseFloat(style.fontSize),
          formSelect: element.matches(formSelectSelector),
          inVerticalScroller,
          visibleY: inVerticalScroller && visibleBottom > visibleTop ? { top: visibleTop, bottom: visibleBottom } : null,
          horizontalScroller,
        })
      }
      return { viewport: { width: window.innerWidth, height: window.innerHeight }, controls, exempt }
    },
    {
      selector: TAPPABLE_CONTROL_SELECTOR,
      formSelectSelector: FORM_SELECT_CONTROL_SELECTOR,
      exemptSubtree: EXEMPT_SUBTREE_SELECTOR,
      exemptRole: EXEMPT_ROLE_SELECTOR,
    },
  )
}

/** 轮询到连续两次枚举结果逐字段相同（面板展开、抽屉落定、字体换入之后）再返回，不量渲染中途的盒子。 */
export async function auditSettledControls(page: Page): Promise<TouchAudit> {
  let previous = ''
  let audit: TouchAudit | null = null
  await expect
    .poll(
      async () => {
        audit = await auditControls(page)
        const serialized = JSON.stringify(audit)
        const settled = serialized === previous
        previous = serialized
        return settled
      },
      { message: '可点控件的枚举结果应落定（连续两次相同）' },
    )
    .toBe(true)
  return audit!
}

function describeControl(control: AuditedControl): string {
  const { x, y, width, height } = control.box
  return `${control.tag}「${control.name}」 ${width}x${height} @ (${x}, ${y})`
}

/** 宽或高不到 44 的控件；空 = 全部达标。 */
export function belowTouchFloor(controls: AuditedControl[]): string[] {
  return controls
    .filter((control) => control.box.width < MIN_TOUCH_TARGET_PX || control.box.height < MIN_TOUCH_TARGET_PX)
    .map(describeControl)
}

/** 字号不到 16px 的 `select` / select 触发器；空 = 全部达标。 */
export function belowFormFontFloor(controls: AuditedControl[]): string[] {
  return controls
    .filter((control) => control.formSelect && control.fontSizePx < MIN_FORM_FONT_PX)
    .map((control) => `${describeControl(control)} font-size ${control.fontSizePx}px`)
}

/**
 * 不在视口内的控件（口径 (4)）：横向取完整点击区；纵向对纵向滚动容器的成员取可见部分
 * （完全滚出视为通过），其余取完整点击区。空 = 全部在视口内。
 */
export function outsideViewport(audit: TouchAudit): string[] {
  const { width, height } = audit.viewport
  return audit.controls
    .filter((control) => {
      const { box } = control
      if (box.x < -EDGE_TOLERANCE_PX || box.x + box.width > width + EDGE_TOLERANCE_PX) return true
      if (control.inVerticalScroller && control.visibleY === null) return false
      const top = control.inVerticalScroller ? control.visibleY!.top : box.y
      const bottom = control.inVerticalScroller ? control.visibleY!.bottom : box.y + box.height
      return top < -EDGE_TOLERANCE_PX || bottom > height + EDGE_TOLERANCE_PX
    })
    .map(describeControl)
}

/** 处在横向可滚动祖先内的控件；不应存在（口径 (4)）。 */
export function insideHorizontalScroller(controls: AuditedControl[]): string[] {
  return controls.filter((control) => control.horizontalScroller !== null).map((control) => `${describeControl(control)} in ${control.horizontalScroller}`)
}

/** 每个状态一行的实测摘要：枚举数、最小宽 / 高、表单选择控件的最小字号。 */
export function summarize(controls: AuditedControl[]) {
  const fonts = controls.filter((control) => control.formSelect).map((control) => control.fontSizePx)
  return {
    count: controls.length,
    minWidth: Math.min(...controls.map((control) => control.box.width)),
    minHeight: Math.min(...controls.map((control) => control.box.height)),
    formSelects: fonts.length,
    minFormFontPx: fonts.length > 0 ? Math.min(...fonts) : null,
  }
}
