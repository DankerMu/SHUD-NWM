import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SiteHeader } from '@/components/layout/SiteHeader'

const V2_TITLE = '全国水文模拟系统（V2.0）'
const HALF_WIDTH_TITLE = '全国水文模拟系统(V2.0)'
/** 头部自身的类名字面量（#2862 改动前的原样）：末端插槽不得改它。 */
const HEADER_CLASS_NAME =
  'flex h-[84px] shrink-0 items-center justify-between gap-4 bg-gradient-to-r from-primary-900 via-primary-800 to-primary-700 px-5 shadow-md mobile:h-12'

describe('SiteHeader V2.0 brand identity', () => {
  it('renders the V2.0 title with full-width parentheses', () => {
    render(<SiteHeader />)

    const title = screen.getByText(V2_TITLE)

    expect(title.textContent).toBe(V2_TITLE)
    // 半角括号形态必须不存在：全角 U+FF08/U+FF09 是品牌口径，混入半角即回归。
    expect(screen.queryByText(HALF_WIDTH_TITLE)).toBeNull()
  })

  it('renders the title at 28px extrabold', () => {
    render(<SiteHeader />)

    const title = screen.getByText(V2_TITLE)

    expect(title).toHaveClass('font-extrabold')
    expect(title).toHaveClass('text-[28px]')
  })

  it('renders the header at the 84px baseline height', () => {
    render(<SiteHeader />)

    expect(screen.getByRole('banner')).toHaveClass('h-[84px]')
  })

  it('renders the sponsor strip enlarged with object-contain', () => {
    render(<SiteHeader />)

    const sponsors = screen.getByAltText('合作单位')

    expect(sponsors).toHaveClass('h-14')
    expect(sponsors).toHaveClass('object-contain')
  })

  // 末端插槽（#2862）只给开发用角色切换器用：不传时头部与改动前逐字相同，不留空容器。
  it('renders exactly the title block and the sponsor strip when no end slot is passed', () => {
    render(<SiteHeader />)

    const header = screen.getByRole('banner')

    expect(header.className).toBe(HEADER_CLASS_NAME)
    expect(header.children).toHaveLength(2)
    expect(header.children[0].tagName).toBe('DIV')
    expect(header.children[0]).toContainElement(screen.getByText(V2_TITLE))
    expect(header.children[1]).toBe(screen.getByAltText('合作单位'))
  })

  it('renders the end slot as the last, non-shrinking child after the sponsor strip', () => {
    render(<SiteHeader endSlot={<button type="button">slot content</button>} />)

    const header = screen.getByRole('banner')

    expect(header.className).toBe(HEADER_CLASS_NAME)
    expect(header.children).toHaveLength(3)
    expect(header.children[1]).toBe(screen.getByAltText('合作单位'))
    expect(header.children[2]).toContainElement(screen.getByRole('button', { name: 'slot content' }))
    expect(header.children[2]).toHaveClass('shrink-0')
  })
})
