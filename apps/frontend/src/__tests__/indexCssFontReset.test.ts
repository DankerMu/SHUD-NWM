import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { findUnlayeredFontResets } from '../../scripts/check-unlayered-font-reset.mjs'

/**
 * 表单控件的字体重置不得写在层外（openspec layer-font-inherit-reset design D4，#2861）：
 * 层外声明压过所有分层声明，`button` / `input` / `select` / `textarea` 上不带 `!` 的 `text-*`、`font-*`、
 * `leading-*` 会静默失效。唯一的重置是 Tailwind preflight 在 `@layer base` 里的那一条。
 * 构建产物的同一项扫描在 `check:bundle`（`scripts/check-unlayered-font-reset.mjs`），不在这里读 `dist/`。
 */
const indexCss = readFileSync(path.resolve(__dirname, '../index.css'), 'utf8')

describe('index.css form-control font reset', () => {
  it('declares no `font` on button / input / select / textarea outside a cascade layer', () => {
    expect(findUnlayeredFontResets(indexCss)).toEqual([])
  })
})

describe('findUnlayeredFontResets', () => {
  it('reports the unlayered reset that #2861 removed', () => {
    expect(findUnlayeredFontResets('a { color: inherit; }\nbutton,\ninput,\nselect,\ntextarea {\n  font: inherit;\n}\n')).toEqual([
      'button, input, select, textarea { font: inherit; }',
    ])
  })

  it('reports it in minified output next to a layered preflight, and inside an unlayered at-rule', () => {
    const minified = '@layer base{button,input,select,optgroup,textarea{font:inherit}}a{color:inherit}button,input,select,textarea{font:inherit}'
    expect(findUnlayeredFontResets(minified)).toEqual(['button,input,select,textarea { font:inherit }'])
    expect(findUnlayeredFontResets('@media (max-width: 767.98px) { .card select { font: 12px/1 sans-serif } }')).toEqual([
      '.card select { font: 12px/1 sans-serif }',
    ])
  })

  it('accepts the layered preflight reset, non-font rules on form controls and font rules on other elements', () => {
    const css = [
      '/* button { font: inherit } */',
      '@layer base { button, input, select, optgroup, textarea { font: inherit; } }',
      '@layer utilities { @media (max-width: 767.98px) { .mobile\\:text-base { font-size: 1rem; } } }',
      ":where(button, a, input, select, textarea, [role='button']):focus-visible { outline: 2px solid #1e88e5; }",
      '.maplibregl-map { font: 12px/20px Helvetica Neue, Arial, sans-serif; }',
      '.maplibregl-ctrl-attrib-button { font-size: 12px; }',
      '.note::before { content: "button { font: inherit }"; }',
      ".before\\:content-\\[\\'\\'\\]:before { --tw-content: ''; }",
    ].join('\n')

    expect(findUnlayeredFontResets(css)).toEqual([])
    // 上面那些规则不会让扫描错位：紧跟其后的层外重置照样被报出。
    expect(findUnlayeredFontResets(`${css}\nselect { font: inherit }`)).toEqual(['select { font: inherit }'])
  })
})
