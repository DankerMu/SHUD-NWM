export const m11VisualTokens = {
  navHeight: '56px',
  leftPanelWidth: '280px',
  rightPanelWidth: '340px',
  rightPanelMinWidth: '320px',
  rightPanelMaxWidth: '360px',
  timelineHeight: '64px',
  spacingUnit: '4px',
  radiusSm: '4px',
  radiusMd: '8px',
  fontBody: '14px',
  fontCaption: '12px',
  fontTitle: '16px',
  fontMetric: '24px',
  warningLevels: {
    normal: '#4FC3F7',
    elevated: '#81C784',
    watch: '#FFD54F',
    warning: '#FFB74D',
    major: '#FF8A65',
    severe: '#E57373',
    extreme: '#AB47BC',
    orange: '#FF9800',
    red: '#F44336',
  },
  statuses: {
    success: '#4CAF50',
    warning: '#FF9800',
    danger: '#F44336',
    info: '#2196F3',
  },
} as const

export type M11WarningLevel = keyof typeof m11VisualTokens.warningLevels
