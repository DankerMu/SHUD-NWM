import { describe, expect, it } from 'vitest'

import { formatDate, formatDuration } from '@/lib/format'

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

describe('formatDate', () => {
  it('formats Date, string, and timestamp inputs', () => {
    const date = new Date(2026, 4, 9, 12, 34, 56)
    const expected = dateFormatter.format(date)

    expect(formatDate(date)).toBe(expected)
    expect(formatDate(date.toISOString())).toBe(expected)
    expect(formatDate(date.getTime())).toBe(expected)
  })

  it('returns a placeholder for null, undefined, zero, and invalid inputs', () => {
    expect(formatDate(null)).toBe('-')
    expect(formatDate(undefined)).toBe('-')
    expect(formatDate(0)).toBe('-')
    expect(formatDate('not-a-date')).toBe('-')
  })
})

describe('formatDuration', () => {
  it('formats second and minute durations', () => {
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(45)).toBe('45s')
    expect(formatDuration(90)).toBe('1m 30s')
    expect(formatDuration(120)).toBe('2m')
  })

  it('formats hour durations', () => {
    expect(formatDuration(3600)).toBe('1h 0m')
    expect(formatDuration(3661)).toBe('1h 1m')
  })

  it('returns a placeholder for null, undefined, and negative durations', () => {
    expect(formatDuration(null)).toBe('-')
    expect(formatDuration(undefined)).toBe('-')
    expect(formatDuration(-1)).toBe('-')
  })
})
