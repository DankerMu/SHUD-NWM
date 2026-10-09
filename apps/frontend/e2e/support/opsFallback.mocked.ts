import { expect, type Locator, type Page } from '@playwright/test'

import type { Box } from './legendLauncher.mocked'
import {
  controlledCycleTime,
  controlledFailedJobId,
  cycleTime,
  mockControlledOpsApi,
  mockMonitoringApi,
  type MonitoringApiMockOptions,
} from './monitoring.mocked'
import { setRole } from './setRole'

/**
 * 运维页兜底（openspec mobile-responsive-display task 6.1，design.md D14）的开页助手与量具。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用（mock 本身在 `monitoring.mocked.ts`）。
 *
 * `/ops` 与 `/monitoring` 是同一个页面组件的两种模式，移动 spec 与桌面 spec 都对两者参数化。
 * 这里只量、不判：返回的数字由 spec 断言；仅有的断言是“取错元素”的防护。
 */

export interface OpsFallbackTarget {
  route: '/ops' | '/monitoring'
  heading: string
  url: string
  /** 任务表里带「查看日志」的那一行。 */
  logRow: RegExp
  logJobId: string
  install: (page: Page, options?: MonitoringApiMockOptions) => Promise<unknown>
}

export const OPS_FALLBACK_TARGETS: OpsFallbackTarget[] = [
  {
    route: '/ops',
    heading: '内部诊断',
    url: `/ops?source=gfs&cycle=${encodeURIComponent(controlledCycleTime)}`,
    logRow: new RegExp(controlledFailedJobId),
    logJobId: controlledFailedJobId,
    install: mockControlledOpsApi,
  },
  {
    route: '/monitoring',
    heading: '监控工作台',
    url: `/monitoring?source=gfs&cycle=${encodeURIComponent(cycleTime)}`,
    logRow: /run-failed/,
    logJobId: 'job-failed',
    install: mockMonitoringApi,
  },
]

/** 又宽又长的日志：120 行、每行约 300 字符，任何受支持视口下日志区都必须双向可滚。 */
export const LONG_LOG_LAST_LINE = 'line-119 END-OF-LOG'
export const LONG_LOG = [
  ...Array.from({ length: 119 }, (_, index) => `line-${String(index).padStart(3, '0')} ${'forecast-stderr-token '.repeat(14)}`),
  LONG_LOG_LAST_LINE,
].join('\n')

/** 两个路由上全部可聚焦的表单控件（`aria-label`）；开发用角色切换器不在其中。 */
export const FORM_CONTROL_LABELS = ['Source', 'Status filter', 'Run type filter', 'Scenario filter', 'Cycle Time UTC'] as const
export const ROLE_TRIGGER_SELECTOR = '[role=combobox][aria-label="Role"]'
export const FORM_CONTROL_SELECTOR = 'select, [role=combobox], input'

/** 页面上的六张卡片（`ui/card.tsx` 的 `Card`），按 DOM 顺序；最后一张是页面的最后一块内容。 */
export const CARD_TITLES = ['当前周期', '作业计数', 'Slurm 队列深度', '七阶段流水线', '作业列表', '趋势'] as const

/**
 * `Card` 根元素的识别：`ui/card.tsx` 给它的三个类同时出现。不加测试属性——页面滚动容器内
 * 没有别的元素同时带这三个类；量到的卡片标题必须恰为 `CARD_TITLES`（spec 断言），取错即红。
 */
const CARD_SELECTOR = '[class~="shadow-[var(--shadow-md)]"][class~="bg-panel"][class~="border"]'

/** 装好 mock，以 operator 角色打开运维页并等到六张卡片的数据就绪。 */
export async function openOpsFallbackPage(page: Page, target: OpsFallbackTarget, options: MonitoringApiMockOptions = {}) {
  await target.install(page, options)
  await visitOpsFallbackPage(page, target)
}

/**
 * 导航到运维页（mock 已装好）、切到 operator 并等到数据就绪。角色不跨导航保留，每次导航后都重新切。
 * 用例内换视口后要量页面的，先 `setViewportSize` 再调用这里重新加载，不要在已加载的页面上直接改视口：
 * echarts-for-react 会吞掉图表初始化后的第一次尺寸回调，视口若恰在那之前变化，趋势图就永久停在旧宽度上、
 * 把页面滚动容器撑出横向溢出（改动前后都能复现的既有竞态，与本 task 的样式无关）。
 */
export async function visitOpsFallbackPage(page: Page, target: OpsFallbackTarget) {
  await page.goto(target.url)
  await expect(page.getByText('权限不足')).toBeVisible()
  await setRole(page, 'operator')
  await expect(page.getByRole('heading', { name: target.heading })).toBeVisible()
  await expect(page.getByRole('row', { name: target.logRow })).toBeVisible()
  await expect(page.getByRole('heading', { name: '趋势' })).toBeVisible()
  // 换角色后门禁有短暂过渡态：那时 main 的最后一个子元素还不是页面滚动容器，所以轮询。
  await expect.poll(async () => (await measureOpsFallbackPage(page))?.scroller.overflowY ?? null).toBe('auto')
}

export interface NamedBox {
  name: string
  box: Box
}

export interface OpsFallbackPageMeasure {
  viewport: { width: number; height: number }
  /** 页面滚动容器 = `main` 的最后一个子元素。 */
  scroller: {
    overflowY: string
    paddingLeft: number
    paddingRight: number
    scrollWidth: number
    clientWidth: number
    scrollHeight: number
    clientHeight: number
    scrollTop: number
  }
  /** 页面滚动容器的可见直接子元素（标题、筛选条、告警条、概要区、主网格）。 */
  children: NamedBox[]
  cards: NamedBox[]
  /** 互不为祖先 / 后代、纵向区间重叠超过 1px 的卡片对。 */
  sideBySideCards: string[]
  /** 主网格与概要区外层网格的列数（计算后的 `grid-template-columns` 轨道数，含隐式列）。 */
  mainGridColumns: number
  summaryGridColumns: number
  /** 任务表自己的容器（`ui/table.tsx` 的包装 `div`）。 */
  tableContainer: { overflowX: string; childTag: string; scrollWidth: number; clientWidth: number }
  windowScrollY: number
  documentScrollTop: number
  /** 文档滚动元素的纵向溢出量 `scrollHeight − clientHeight`：> 0 即窗口可滚。 */
  documentOverflowY: number
  /** `main`（页面滚动容器的父元素）：它不该是滚动者。 */
  main: { overflowY: string; scrollHeight: number; clientHeight: number; scrollTop: number }
}

/** 量页面；页面滚动容器、两个网格或任务表容器任一缺失时返回 null（供轮询等待）。 */
export async function measureOpsFallbackPage(page: Page): Promise<OpsFallbackPageMeasure | null> {
  return page.evaluate((cardSelector) => {
    const main = document.querySelector('main')
    const scroller = main?.lastElementChild
    const scrollingElement = document.scrollingElement
    if (!main || !(scroller instanceof HTMLElement) || !scrollingElement) return null
    const mainGrid = scroller.lastElementChild
    const summaryGrid = scroller.querySelector(':scope > section')
    const tableContainer = scroller.querySelector('table')?.parentElement
    if (!(mainGrid instanceof HTMLElement) || !(summaryGrid instanceof HTMLElement) || !(tableContainer instanceof HTMLElement)) return null

    const boxOf = (element: Element) => {
      const rect = element.getBoundingClientRect()
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
    }
    const nameOf = (element: Element) =>
      (element.querySelector('h1, h3')?.textContent ?? element.textContent ?? '').trim().slice(0, 24) || element.tagName.toLowerCase()
    const columnsOf = (grid: HTMLElement) => {
      const style = getComputedStyle(grid)
      if (style.display !== 'grid') return 0
      return style.gridTemplateColumns.split(/\s+/).filter((track) => track !== '' && track !== 'none').length
    }

    const cardElements = Array.from(scroller.querySelectorAll(cardSelector))
    const cards = cardElements.map((element) => ({ name: (element.querySelector('h3')?.textContent ?? '').trim(), box: boxOf(element) }))
    const sideBySideCards: string[] = []
    cardElements.forEach((first, i) => {
      cardElements.slice(i + 1).forEach((second, offset) => {
        if (first.contains(second) || second.contains(first)) return
        const a = cards[i].box
        const b = cards[i + 1 + offset].box
        const overlap = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
        if (overlap > 1) sideBySideCards.push(`${cards[i].name} | ${cards[i + 1 + offset].name}`)
      })
    })

    const scrollerStyle = getComputedStyle(scroller)
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      scroller: {
        overflowY: scrollerStyle.overflowY,
        paddingLeft: Number.parseFloat(scrollerStyle.paddingLeft),
        paddingRight: Number.parseFloat(scrollerStyle.paddingRight),
        scrollWidth: scroller.scrollWidth,
        clientWidth: scroller.clientWidth,
        scrollHeight: scroller.scrollHeight,
        clientHeight: scroller.clientHeight,
        scrollTop: scroller.scrollTop,
      },
      children: Array.from(scroller.children)
        .map((element) => ({ name: nameOf(element), box: boxOf(element) }))
        .filter(({ box }) => box.width > 0 && box.height > 0),
      cards,
      sideBySideCards,
      mainGridColumns: columnsOf(mainGrid),
      summaryGridColumns: columnsOf(summaryGrid),
      tableContainer: {
        overflowX: getComputedStyle(tableContainer).overflowX,
        childTag: tableContainer.firstElementChild?.tagName.toLowerCase() ?? '',
        scrollWidth: tableContainer.scrollWidth,
        clientWidth: tableContainer.clientWidth,
      },
      windowScrollY: window.scrollY,
      documentScrollTop: scrollingElement.scrollTop,
      documentOverflowY: scrollingElement.scrollHeight - scrollingElement.clientHeight,
      main: {
        overflowY: getComputedStyle(main).overflowY,
        scrollHeight: main.scrollHeight,
        clientHeight: main.clientHeight,
        scrollTop: main.scrollTop,
      },
    }
  }, CARD_SELECTOR)
}

/** 量页面并校验没有取错元素：滚动容器 `overflow-y: auto`、卡片恰为六张、任务表容器的直接子元素是 `table`。 */
export async function measureCheckedOpsFallbackPage(page: Page): Promise<OpsFallbackPageMeasure> {
  const measure = await measureOpsFallbackPage(page)
  expect(measure, '页面滚动容器、两个网格与任务表容器都应存在').not.toBeNull()
  expect(measure!.scroller.overflowY, '页面滚动容器（main 的最后一个子元素）的 overflow-y').toBe('auto')
  expect(measure!.cards.map(({ name }) => name), '页面上的卡片').toEqual([...CARD_TITLES])
  expect(measure!.tableContainer.childTag, '任务表容器的直接子元素').toBe('table')
  expect(measure!.mainGridColumns, '主网格应是 grid').toBeGreaterThan(0)
  expect(measure!.summaryGridColumns, '概要区外层应是 grid').toBeGreaterThan(0)
  return measure!
}

/** 左边 < `minPadding` 或右边 > 视口宽 − `minPadding` 的卡片与页面容器直接子元素；空 = 都在内边距内。 */
export function outsidePagePadding(measure: OpsFallbackPageMeasure, minPadding: number, tolerance = 0.5): string[] {
  const rightLimit = measure.viewport.width - minPadding
  return [...measure.cards.map((item) => ({ ...item, kind: '卡片' })), ...measure.children.map((item) => ({ ...item, kind: '子元素' }))]
    .filter(({ box }) => box.x < minPadding - tolerance || box.x + box.width > rightLimit + tolerance)
    .map(({ kind, name, box }) => `${kind}「${name}」left=${box.x} right=${box.x + box.width}（应在 ${minPadding}..${rightLimit} 内）`)
}

/** 把页面滚动容器滚到底（直接设它自己的 scrollTop；窗口与外壳不参与）。 */
export async function scrollOpsFallbackPageToEnd(page: Page) {
  await page.evaluate(() => {
    const scroller = document.querySelector('main')?.lastElementChild
    if (!(scroller instanceof HTMLElement)) throw new Error('page scroll container under main was not found')
    scroller.scrollTop = scroller.scrollHeight
  })
}

/**
 * 主动去滚窗口与 `main`：`window.scrollTo`、文档滚动元素与 `main` 的 `scrollTop` 各写一次。
 * 只设页面滚动容器自己的 scrollTop 永远动不了窗口；这里的写入在文档或 `main` 可滚时一定生效，读数由 spec 轮询。
 */
export async function tryScrollWindowAndMain(page: Page, offset: number) {
  await page.evaluate((top) => {
    const main = document.querySelector('main')
    const scrollingElement = document.scrollingElement
    if (!main || !scrollingElement) throw new Error('main or the document scrolling element was not found')
    window.scrollTo(0, top)
    scrollingElement.scrollTop = top
    main.scrollTop = top
  }, offset)
}

export interface FormControlMeasure {
  name: string
  fontSizePx: number
  height: number
}

/** 量一组控件的计算字号与高度：只留可见的（有布局盒、`visibility` 不是 hidden）。 */
export async function measureFormControls(locator: Locator): Promise<FormControlMeasure[]> {
  return locator.evaluateAll((elements) =>
    elements.flatMap((element) => {
      const rect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden') return []
      const name = (element.getAttribute('aria-label') ?? element.textContent ?? '').trim() || element.tagName.toLowerCase()
      return [{ name, fontSizePx: Number.parseFloat(style.fontSize), height: rect.height }]
    }),
  )
}

/** 按 `aria-label` 点名量五个表单控件；缺任何一个都在这里报错。 */
export async function measureNamedFormControls(page: Page): Promise<FormControlMeasure[]> {
  const measured: FormControlMeasure[] = []
  for (const label of FORM_CONTROL_LABELS) {
    const controls = await measureFormControls(page.locator(`[aria-label="${label}"]`))
    expect(controls, `表单控件「${label}」应恰有一个可见节点`).toHaveLength(1)
    measured.push(controls[0])
  }
  return measured
}

/** 打开失败作业的日志弹窗并等到长日志渲染出来。目标行由 Playwright 自动滚入视野，不用 `force`。 */
export async function openLogDialog(page: Page, target: OpsFallbackTarget) {
  await page.getByRole('row', { name: target.logRow }).getByRole('button', { name: /查看日志/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText(`作业日志 ${target.logJobId}`)
  await expect(dialog.locator('pre')).toContainText(LONG_LOG_LAST_LINE)
}

export interface LogDialogMeasure {
  viewport: { width: number; height: number }
  dialog: Box
  /** 弹窗的内容框：包围盒去掉边框与内边距，日志区必须画在它里面。 */
  dialogContent: Box
  /** 弹窗自身的纵向滚动量：日志区画到弹窗框外时 `scrollHeight > clientHeight`。 */
  dialogScroll: { scrollHeight: number; clientHeight: number; overflowY: string }
  close: Box
  /** 标题文字逐行的包围盒（`Range.getClientRects`），不是标题元素的盒——后者含右内边距、恒伸到关闭按钮底下。 */
  titleLines: Box[]
  log: Box
  logScroll: { scrollWidth: number; clientWidth: number; scrollHeight: number; clientHeight: number; overflowX: string; overflowY: string }
  dialogMaxHeight: string
  logMaxHeight: string
}

export async function measureLogDialog(page: Page): Promise<LogDialogMeasure | null> {
  return page.getByRole('dialog').evaluate((dialog) => {
    const close = Array.from(dialog.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Close')
    const log = dialog.querySelector('pre')
    const title = dialog.querySelector('h2')
    if (!close || !log || !title) return null
    const titleRange = document.createRange()
    titleRange.selectNodeContents(title)
    const boxOf = (element: Element) => {
      const rect = element.getBoundingClientRect()
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
    }
    const dialogStyle = getComputedStyle(dialog)
    const logStyle = getComputedStyle(log)
    const dialogBox = boxOf(dialog)
    const px = (value: string) => Number.parseFloat(value) || 0
    const inset = {
      left: px(dialogStyle.borderLeftWidth) + px(dialogStyle.paddingLeft),
      right: px(dialogStyle.borderRightWidth) + px(dialogStyle.paddingRight),
      top: px(dialogStyle.borderTopWidth) + px(dialogStyle.paddingTop),
      bottom: px(dialogStyle.borderBottomWidth) + px(dialogStyle.paddingBottom),
    }
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      dialog: dialogBox,
      dialogContent: {
        x: dialogBox.x + inset.left,
        y: dialogBox.y + inset.top,
        width: dialogBox.width - inset.left - inset.right,
        height: dialogBox.height - inset.top - inset.bottom,
      },
      dialogScroll: { scrollHeight: dialog.scrollHeight, clientHeight: dialog.clientHeight, overflowY: dialogStyle.overflowY },
      close: boxOf(close),
      titleLines: Array.from(titleRange.getClientRects())
        .filter((rect) => rect.width > 0 && rect.height > 0)
        .map((rect) => ({ x: rect.x, y: rect.y, width: rect.width, height: rect.height })),
      log: boxOf(log),
      logScroll: {
        scrollWidth: log.scrollWidth,
        clientWidth: log.clientWidth,
        scrollHeight: log.scrollHeight,
        clientHeight: log.clientHeight,
        overflowX: logStyle.overflowX,
        overflowY: logStyle.overflowY,
      },
      dialogMaxHeight: dialogStyle.maxHeight,
      logMaxHeight: logStyle.maxHeight,
    }
  })
}

/**
 * 日志弹窗不达标的条目（口径 (7)）；空 = 达标：弹窗与关闭按钮的包围盒在视口内、日志区的包围盒在弹窗的
 * 内容框内、日志区自己双向可滚、弹窗自身不滚、标题文字不与关闭按钮相交。
 * 日志区对的是内容框而不是包围盒：改动前 750×342 下日志区只越出内容框约 5px、吃进下内边距，
 * 仍在包围盒内——对包围盒断言看不出这次越界。
 */
export function logDialogViolations(measure: LogDialogMeasure, tolerance = 0.5): string[] {
  const viewportBox: Box = { x: 0, y: 0, width: measure.viewport.width, height: measure.viewport.height }
  const inside = (outer: Box, inner: Box) =>
    inner.x >= outer.x - tolerance &&
    inner.y >= outer.y - tolerance &&
    inner.x + inner.width <= outer.x + outer.width + tolerance &&
    inner.y + inner.height <= outer.y + outer.height + tolerance
  const describe = (box: Box) => `x=${box.x} y=${box.y} w=${box.width} h=${box.height}`
  const violations: string[] = []
  if (!inside(viewportBox, measure.dialog)) violations.push(`弹窗超出视口：${describe(measure.dialog)}`)
  if (!inside(viewportBox, measure.close)) violations.push(`关闭按钮超出视口：${describe(measure.close)}`)
  if (!inside(measure.dialogContent, measure.log)) {
    violations.push(`日志区画到弹窗内容框外：日志区 ${describe(measure.log)}，内容框 ${describe(measure.dialogContent)}`)
  }
  // 标题文字与关闭按钮：严格相交（两个方向的重叠都 > 0）才算；达标时两者横向隔着标题的右内边距。
  if (measure.titleLines.length === 0) violations.push('没有量到标题文字')
  for (const line of measure.titleLines) {
    const overlapX = Math.min(line.x + line.width, measure.close.x + measure.close.width) - Math.max(line.x, measure.close.x)
    const overlapY = Math.min(line.y + line.height, measure.close.y + measure.close.height) - Math.max(line.y, measure.close.y)
    if (overlapX > 0 && overlapY > 0) violations.push(`标题文字伸到关闭按钮底下：文字 ${describe(line)}，关闭按钮 ${describe(measure.close)}`)
  }
  const { logScroll, dialogScroll } = measure
  if (logScroll.scrollWidth <= logScroll.clientWidth) violations.push(`日志区不能横向滚动：${logScroll.scrollWidth} / ${logScroll.clientWidth}`)
  if (logScroll.scrollHeight <= logScroll.clientHeight) violations.push(`日志区不能纵向滚动：${logScroll.scrollHeight} / ${logScroll.clientHeight}`)
  if (dialogScroll.scrollHeight > dialogScroll.clientHeight) {
    violations.push(`弹窗自身有纵向溢出（滚动者应是日志区）：${dialogScroll.scrollHeight} / ${dialogScroll.clientHeight}`)
  }
  return violations
}
