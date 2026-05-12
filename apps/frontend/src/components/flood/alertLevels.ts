import type { ExpressionSpecification, LineLayerSpecification } from 'maplibre-gl'

export type AlertLevel =
  | 'normal'
  | 'elevated'
  | 'watch'
  | 'warning'
  | 'high_risk'
  | 'severe'
  | 'extreme'

export const ALERT_LEVELS: AlertLevel[] = [
  'extreme',
  'severe',
  'high_risk',
  'warning',
  'watch',
  'elevated',
  'normal',
]

export const ALERT_LEVEL_META: Record<
  AlertLevel,
  { label: string; range: string; color: string; minReturnPeriod: number }
> = {
  normal: { label: '正常', range: 'T<2', color: '#808080', minReturnPeriod: 0 },
  elevated: { label: '偏高', range: '2≤T<5', color: '#4A90D9', minReturnPeriod: 2 },
  watch: { label: '关注', range: '5≤T<10', color: '#FFD700', minReturnPeriod: 5 },
  warning: { label: '警戒', range: '10≤T<20', color: '#FF8C00', minReturnPeriod: 10 },
  high_risk: { label: '高风险', range: '20≤T<50', color: '#FF4500', minReturnPeriod: 20 },
  severe: { label: '严重', range: '50≤T<100', color: '#DC143C', minReturnPeriod: 50 },
  extreme: { label: '极端', range: 'T≥100', color: '#800080', minReturnPeriod: 100 },
}

export const SUPER_WARNING_LEVELS = new Set<AlertLevel>(['warning', 'high_risk', 'severe', 'extreme'])

export const FLOOD_TILE_SOURCE_ID = 'flood-return-period'
export const FLOOD_TILE_LAYER_ID = 'flood-return-period-line'
export const FLOOD_TILE_HOVER_LAYER_ID = 'flood-return-period-hover'
export const FLOOD_TILE_SELECTED_LAYER_ID = 'flood-return-period-selected'
export const FLOOD_TILE_SOURCE_LAYER = 'flood_return_period'

export function isAlertLevel(value: unknown): value is AlertLevel {
  return typeof value === 'string' && value in ALERT_LEVEL_META
}

export function alertLevelLabel(level: string | null | undefined) {
  return isAlertLevel(level) ? ALERT_LEVEL_META[level].label : '无曲线'
}

export function alertLevelColor(level: string | null | undefined) {
  return isAlertLevel(level) ? ALERT_LEVEL_META[level].color : '#CCCCCC'
}

export const floodLineColorExpression: ExpressionSpecification = [
  'match',
  ['coalesce', ['get', 'warning_level'], 'unavailable'],
  'normal',
  ALERT_LEVEL_META.normal.color,
  'elevated',
  ALERT_LEVEL_META.elevated.color,
  'watch',
  ALERT_LEVEL_META.watch.color,
  'warning',
  ALERT_LEVEL_META.warning.color,
  'high_risk',
  ALERT_LEVEL_META.high_risk.color,
  'severe',
  ALERT_LEVEL_META.severe.color,
  'extreme',
  ALERT_LEVEL_META.extreme.color,
  '#CCCCCC',
]

export const floodLineWidthExpression: ExpressionSpecification = [
  'interpolate',
  ['linear'],
  ['coalesce', ['get', 'return_period'], 0],
  0,
  1.2,
  2,
  1.8,
  10,
  2.8,
  20,
  4,
  50,
  5.5,
  100,
  7,
]

export function floodTileLayerPaint(selectedLevel?: AlertLevel | null): LineLayerSpecification['paint'] {
  return {
    'line-color': floodLineColorExpression,
    'line-width': floodLineWidthExpression,
    'line-opacity': selectedLevel
      ? ['case', ['==', ['get', 'warning_level'], selectedLevel], 0.96, 0.18]
      : ['case', ['has', 'warning_level'], 0.86, 0.45],
    'line-dasharray': ['case', ['has', 'warning_level'], ['literal', [1, 0]], ['literal', [2, 2]]],
  }
}
