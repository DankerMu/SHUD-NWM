/**
 * Production C4 DOM helpers. These functions are serialized into the live
 * Playwright page via page.evaluate and also executed against a jsdom/document
 * seam in unit tests. They close over nothing from Node and never synthesize
 * success from a fake.
 */

export interface C4HomeDomObservation {
  mapPresent: boolean
  mapWidth: number
  mapHeight: number
  loading: boolean
  empty: boolean
  mapUnavailable: boolean
  mapSourceError: boolean
}

export interface C4OpsDomObservation {
  headingObserved: boolean
  permissionDenied: boolean
  runtimeUnavailable: boolean
  roleSelectorCount: number
  retryCancelControlCount: number
  queueReadonlyVisible: boolean
  operatorRecoveryVisible: boolean
}

export function inspectC4HomeInPage(): C4HomeDomObservation {
  const map = document.querySelector('[data-testid="m11-map-surface"]') as HTMLElement | null
  const rect = map && typeof map.getBoundingClientRect === 'function'
    ? map.getBoundingClientRect()
    : { width: 0, height: 0 }
  return {
    mapPresent: Boolean(map),
    mapWidth: rect.width,
    mapHeight: rect.height,
    loading: Boolean(document.querySelector('[data-testid="m11-overview-loading"]')),
    empty: Boolean(document.querySelector('[data-testid="m11-overview-empty"]')),
    mapUnavailable: Boolean(document.querySelector('[data-testid="m11-map-unavailable"]')),
    mapSourceError: Boolean(document.querySelector('[data-testid="m11-map-source-error"]')),
  }
}

export function inspectC4OpsInPage(): C4OpsDomObservation {
  const textOf = (node: Element | null): string => (node?.textContent ?? '').trim()
  const headings = Array.from(document.querySelectorAll('h1'))
  const headingObserved = headings.some((el) => textOf(el).includes('内部诊断'))
  const alerts = Array.from(document.querySelectorAll('[role="alert"]'))
  const permissionDenied = alerts.some((el) => textOf(el).includes('权限不足'))
  const statuses = Array.from(document.querySelectorAll('[role="status"]'))
  const runtimeUnavailable = statuses.some((el) => textOf(el).includes('runtime config 不可用'))
  const roleSelectorCount = document.querySelectorAll('[aria-label="Role"]').length
  const buttons = Array.from(document.querySelectorAll('button'))
  const retryCancelControlCount = buttons.filter((button) => /重试|取消/.test(textOf(button))).length
  const queueReadonlyVisible = statuses.some((el) => textOf(el).includes('display_readonly 只读展示节点不读取 Slurm 队列深度'))
    || Array.from(document.querySelectorAll('h3, [class]')).some((el) => textOf(el).includes('队列深度不可用'))
  const operatorRecoveryVisible = Boolean(document.querySelector('[data-testid="ops-manual-recovery-guidance"]'))
    || statuses.some((el) => textOf(el).includes('需要恢复时在 22 compute-control'))
  return {
    headingObserved,
    permissionDenied,
    runtimeUnavailable,
    roleSelectorCount,
    retryCancelControlCount,
    queueReadonlyVisible,
    operatorRecoveryVisible,
  }
}

export function openC4JobLogInPage(jobId: string): { clicked: boolean } {
  if (typeof jobId !== 'string' || jobId.length === 0) return { clicked: false }
  const textOf = (node: Element | null): string => (node?.textContent ?? '').trim()
  const rows = Array.from(document.querySelectorAll('tr'))
  const row = rows.find((candidate) => textOf(candidate).includes(jobId))
  if (!row) return { clicked: false }
  const button = Array.from(row.querySelectorAll('button')).find((candidate) => textOf(candidate).includes('查看日志'))
  if (!button) return { clicked: false }
  button.click()
  return { clicked: true }
}
