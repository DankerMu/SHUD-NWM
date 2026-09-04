import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SiteHeader } from '@/components/layout/SiteHeader'

const V2_TITLE = '全国水文模拟系统（V2.0）'
const HALF_WIDTH_TITLE = '全国水文模拟系统(V2.0)'

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
})
