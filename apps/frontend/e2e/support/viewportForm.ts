import type { Page } from '@playwright/test'

/**
 * 移动形态判据（openspec mobile-responsive-display design.md D1）的测试侧镜像。
 * 移动 spec 在三个移动 project 下都要通过，期望按“是否矮视口横屏”分支时统一走这里，
 * 不各自发明跳过方式。
 */

export type ViewportSize = { width: number; height: number }

export const MOBILE_FORM_MAX_WIDTH = 768
export const MOBILE_FORM_MAX_HEIGHT = 500

/** 移动形态 = 视口宽 < 768px 或高 < 500px。 */
export function isMobileForm(viewport: ViewportSize): boolean {
  return viewport.width < MOBILE_FORM_MAX_WIDTH || viewport.height < MOBILE_FORM_MAX_HEIGHT
}

/** 矮视口横屏 = 移动形态且高 < 500px 横屏。 */
export function isShortLandscape(viewport: ViewportSize): boolean {
  return isMobileForm(viewport) && viewport.height < MOBILE_FORM_MAX_HEIGHT && viewport.width > viewport.height
}

/** 当前页面视口；project 关掉了固定视口（viewport: null）时无从判定形态，直接报错而不是猜。 */
export function requireViewport(page: Page): ViewportSize {
  const viewport = page.viewportSize()
  if (!viewport) {
    throw new Error('page.viewportSize() is null: viewport form needs a fixed viewport (project `use.viewport` or setViewportSize).')
  }
  return viewport
}
