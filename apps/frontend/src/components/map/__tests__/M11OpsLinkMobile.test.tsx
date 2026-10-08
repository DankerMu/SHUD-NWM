import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { M11OpsLink } from '@/components/map/M11FloatingControls'

/**
 * 运维入口的形态分支（openspec mobile-responsive-display task 3.5，design.md D8）：
 * 移动形态 = 与三个启动器同形的 44×44 图标链接，文字「运维」留在 `sr-only` 子节点里；
 * 桌面形态逐字不变。
 */

/** 桌面入口改动前的完整 className（对未改动的源码跑出来就是这一串）。 */
const DESKTOP_CLASS_NAME =
  'absolute right-4 top-28 z-[120] flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-neutral-700 transition-colors hover:bg-white/70 rounded-lg border border-white/40 bg-white/70 shadow-lg backdrop-blur-md supports-[backdrop-filter]:bg-white/55'

function renderLink(props: { visible?: boolean; mobile?: boolean } = {}) {
  const { visible = true, ...rest } = props
  return render(
    <MemoryRouter>
      <M11OpsLink visible={visible} {...rest} />
    </MemoryRouter>,
  )
}

function classes(element: Element) {
  return (element.getAttribute('class') ?? '').split(/\s+/)
}

/** 链接自己名下的文本节点（不含子元素里的文字）。 */
function ownText(element: HTMLElement) {
  return [...element.childNodes]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent ?? '')
    .join('')
    .trim()
}

describe('M11OpsLink mobile form', () => {
  it('is a 44×44 icon link shaped like the launchers, ordered last in the column', () => {
    renderLink({ mobile: true })

    const link = screen.getByTestId('m11-ops-link')
    expect(link.tagName).toBe('A')
    expect(link).toHaveAttribute('href', '/ops')
    const tokens = classes(link)
    expect(tokens).toEqual(expect.arrayContaining(['order-last', 'shrink-0', 'flex', 'h-11', 'w-11', 'items-center', 'justify-center']))
    // 桌面的内边距 / 字号 / 图标间距会让它宽于 44px 或挤掉图标，移动分支不得带。
    for (const token of ['px-3', 'py-2', 'gap-1.5', 'text-xs', 'absolute', 'right-4', 'top-28', 'z-[120]']) {
      expect(tokens).not.toContain(token)
    }
    // 链接自身不能是 sr-only：它得可见、可点。
    expect(tokens).not.toContain('sr-only')

    const icon = link.querySelector('svg')
    expect(icon).not.toBeNull()
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(classes(icon!)).toEqual(expect.arrayContaining(['h-5', 'w-5']))
  })

  it('keeps 「运维」 as its accessible name through a visually hidden child, not as visible text', () => {
    renderLink({ mobile: true })

    const link = screen.getByRole('link', { name: '运维' })
    expect(link).toHaveAttribute('data-testid', 'm11-ops-link')
    expect(link).not.toHaveAttribute('aria-label')
    expect(link).toHaveTextContent('运维')
    expect(ownText(link)).toBe('')
    const hidden = link.querySelectorAll('span.sr-only')
    expect(hidden).toHaveLength(1)
    expect(hidden[0].parentElement).toBe(link)
    expect(hidden[0]).toHaveTextContent(/^运维$/)
  })

  it('renders nothing when the role cannot see it', () => {
    renderLink({ visible: false, mobile: true })
    expect(screen.queryByTestId('m11-ops-link')).toBeNull()
    expect(screen.queryByRole('link')).toBeNull()
  })
})

describe('M11OpsLink desktop form', () => {
  it.each([
    ['mobile omitted', {}],
    ['mobile false', { mobile: false }],
  ])('is unchanged: same class string, icon and visible text (%s)', (_name, props) => {
    renderLink(props)

    const link = screen.getByRole('link', { name: '运维' })
    expect(link).toHaveAttribute('data-testid', 'm11-ops-link')
    expect(link).toHaveAttribute('href', '/ops')
    expect(link.className).toBe(DESKTOP_CLASS_NAME)
    expect(ownText(link)).toBe('运维')
    expect(link.querySelector('.sr-only')).toBeNull()
    expect(link.children).toHaveLength(1)
    const icon = link.querySelector('svg')
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(classes(icon!)).toEqual(expect.arrayContaining(['h-3.5', 'w-3.5']))
  })
})
