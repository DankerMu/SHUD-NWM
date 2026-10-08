import { expect, type Page } from '@playwright/test'

/**
 * 经开发用角色切换器切换角色（openspec mobile-responsive-display task 2.5）。
 *
 * 走真实 UI：对触发器与选项各做一次 `locator.click()`——不带 `force`、不用 `tap()`，
 * 所以 Playwright 的可操作性检查（可见、稳定、未被遮挡）照常生效，切换器被盖住时这里就会失败。
 * 成功判据是触发器文本等于该角色的显示名；桌面形态与移动形态下都成立
 * （移动形态的紧凑触发器只做视觉截断，显示名仍在 DOM 里）。
 *
 * 前置：页面已导航，且构建开启了 role override（mocked 车道的 dev server 即如此）。
 * 找不到切换器、弹层没展开、选项点不到或切换后文本不符都抛出带原因的错误。
 */
export type RoleValue = 'viewer' | 'analyst' | 'operator' | 'model_admin' | 'sys_admin'

/** 与 `src/components/layout/AppShell.tsx` 的 `roleOptions` 一一对应。 */
export const ROLE_LABELS: Record<RoleValue, string> = {
  viewer: 'Viewer',
  analyst: 'Analyst',
  operator: 'Operator',
  model_admin: 'Model Admin',
  sys_admin: 'Sys Admin',
}

const STEP_TIMEOUT_MS = 10_000

function reason(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export async function setRole(page: Page, role: RoleValue): Promise<void> {
  const label = ROLE_LABELS[role]
  if (label === undefined) {
    throw new Error(`setRole: unknown role ${JSON.stringify(role)}; expected one of ${Object.keys(ROLE_LABELS).join(', ')}.`)
  }

  const trigger = page.getByLabel('Role')
  try {
    await trigger.waitFor({ state: 'visible', timeout: STEP_TIMEOUT_MS })
  } catch (error) {
    throw new Error(
      `setRole(${role}): role selector (aria-label "Role") is not visible on ${page.url()} — ` +
        `is the build running with VITE_ENABLE_ROLE_OVERRIDE=true? Cause: ${reason(error)}`,
    )
  }

  try {
    await trigger.click({ timeout: STEP_TIMEOUT_MS })
  } catch (error) {
    throw new Error(`setRole(${role}): role selector trigger could not be clicked (covered or not actionable). Cause: ${reason(error)}`)
  }

  // 限定在 Radix 弹层（role=listbox）里：页面上别的原生 <select> 也有 option。
  const option = page.getByRole('listbox').getByRole('option', { name: label, exact: true })
  try {
    await option.waitFor({ state: 'visible', timeout: STEP_TIMEOUT_MS })
  } catch (error) {
    throw new Error(`setRole(${role}): option "${label}" did not appear after clicking the role selector. Cause: ${reason(error)}`)
  }

  try {
    await option.click({ timeout: STEP_TIMEOUT_MS })
  } catch (error) {
    throw new Error(`setRole(${role}): option "${label}" could not be clicked (outside the viewport or covered). Cause: ${reason(error)}`)
  }

  try {
    await expect(trigger).toHaveText(label, { timeout: STEP_TIMEOUT_MS })
  } catch (error) {
    throw new Error(`setRole(${role}): role selector does not show "${label}" after selecting it. Cause: ${reason(error)}`)
  }
}
