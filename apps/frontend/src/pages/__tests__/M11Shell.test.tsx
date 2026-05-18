import { render, screen } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { m11VisualTokens } from '@/lib/m11/visualTokens'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'

describe('M11 visual foundation shell', () => {
  beforeEach(() => {
    useOverviewDataStore.setState({
      ...useOverviewDataStore.getInitialState(),
      loadOverview: vi.fn().mockResolvedValue(undefined),
      loadBasinDetail: vi.fn().mockResolvedValue(undefined),
    })
  })

  it('exposes mapped layout tokens for nav, panels, timeline, and warning colors', () => {
    window.history.pushState({}, '', '/overview?warningLevel=major')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    const shell = screen.getByTestId('m11-shell')
    expect(shell).toHaveStyle({
      '--m11-left-panel-width': '280px',
      '--m11-right-panel-width': '340px',
      '--m11-timeline-height': '64px',
    })
    expect(m11VisualTokens.navHeight).toBe('56px')
    expect(m11VisualTokens.warningLevels.major).toBe('#FF8A65')
    expect(screen.getByLabelText('M11 左侧面板')).toBeInTheDocument()
    expect(screen.getByLabelText('M11 右侧面板')).toBeInTheDocument()
    expect(screen.getByLabelText('M11 时间轴')).toBeInTheDocument()
  })
})
