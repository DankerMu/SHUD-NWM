import { describe, expect, it } from 'vitest'

import { formatDate, formatDuration, formatUnitForDisplay } from '@/lib/format'

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

describe('formatUnitForDisplay', () => {
  it('lifts the exponent so a flow unit reads as m³/s, not m3/s', () => {
    expect(formatUnitForDisplay('m3/s')).toBe('m³/s')
    expect(formatUnitForDisplay('m2')).toBe('m²')
    expect(formatUnitForDisplay('mm2')).toBe('mm²')
  })

  it('leaves a unit that carries no exponent untouched', () => {
    expect(formatUnitForDisplay('mm')).toBe('mm')
    expect(formatUnitForDisplay('K')).toBe('K')
    expect(formatUnitForDisplay('W/m2')).toBe('W/m²')
  })

  it('only lifts a digit that follows a letter, so a bare number is left alone', () => {
    expect(formatUnitForDisplay('3')).toBe('3')
    expect(formatUnitForDisplay('/3')).toBe('/3')
    expect(formatUnitForDisplay('10m')).toBe('10m')
  })

  it('lifts nothing but 2 and 3 — a digit it has no glyph for must survive verbatim', () => {
    expect(formatUnitForDisplay('m4')).toBe('m4')
    expect(formatUnitForDisplay('m1')).toBe('m1')
  })

  it('is idempotent, so a value that already carries the superscript is not double-handled', () => {
    expect(formatUnitForDisplay('m³/s')).toBe('m³/s')
    expect(formatUnitForDisplay(formatUnitForDisplay('m3/s'))).toBe('m³/s')
  })

  it('renders an absent unit as the empty string rather than "null"', () => {
    expect(formatUnitForDisplay(null)).toBe('')
    expect(formatUnitForDisplay(undefined)).toBe('')
    expect(formatUnitForDisplay('')).toBe('')
  })
})
