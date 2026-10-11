#!/usr/bin/env node
/**
 * Unlayered form-control font reset scan (#2861, openspec layer-font-inherit-reset design D4).
 *
 * A `font` declaration on `button` / `input` / `select` / `textarea` outside every `@layer` beats all
 * layered CSS, so Tailwind's `text-*` / `font-*` / `leading-*` utilities silently stop working on those
 * elements. The only form-control font reset allowed is preflight's, inside `@layer base`.
 *
 *   node scripts/check-unlayered-font-reset.mjs dist/assets
 *
 * Node stdlib only. `src/__tests__/indexCssFontReset.test.ts` runs the same scan over `src/index.css`.
 */

import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const FORM_CONTROL_SELECTOR = /(^|[\s>+~,(])(button|input|select|textarea)(?![\w-])/
const FONT_DECLARATION = /(^|[;{\s])font\s*:/

/**
 * Returns `selector { declarations }` for every style rule that sits outside all `@layer` blocks, names
 * one of the four form controls as a type selector and declares the `font` shorthand. Empty = clean.
 */
export function findUnlayeredFontResets(css) {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const found = []
  // 每个未闭合的 `{` 一项：at-rule 记它的前导（`@layer base`、`@media …`），样式规则记 null。
  const open = []
  let preludeStart = 0
  let quote = null
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    // 转义在字符串内外都跳过下一个字符：Tailwind 的类名选择器里有 `\'`、`\{` 这样的转义。
    if (char === '\\') {
      index += 1
      continue
    }
    if (quote) {
      if (char === quote) quote = null
      continue
    }
    if (char === '"' || char === "'") {
      quote = char
    } else if (char === ';') {
      preludeStart = index + 1
    } else if (char === '}') {
      open.pop()
      preludeStart = index + 1
    } else if (char === '{') {
      const prelude = source.slice(preludeStart, index).trim()
      const atRule = prelude.startsWith('@')
      if (!atRule && !open.some((entry) => entry !== null && /^@layer\b/.test(entry)) && FORM_CONTROL_SELECTOR.test(prelude)) {
        const end = source.indexOf('}', index)
        const body = source.slice(index + 1, end === -1 ? source.length : end)
        if (FONT_DECLARATION.test(body)) found.push(`${prelude.replace(/\s+/g, ' ')} { ${body.trim().replace(/\s+/g, ' ')} }`)
      }
      open.push(atRule ? prelude : null)
      preludeStart = index + 1
    }
  }
  return found
}

function main(assetsDir) {
  const files = readdirSync(assetsDir).filter((name) => name.endsWith('.css')).sort()
  if (files.length === 0) {
    process.stderr.write(`Unlayered font reset check failed: no CSS assets found in ${assetsDir}.\n`)
    process.exit(1)
  }
  const violations = files.flatMap((name) =>
    findUnlayeredFontResets(readFileSync(path.join(assetsDir, name), 'utf8')).map((rule) => `${name}: ${rule}`),
  )
  if (violations.length > 0) {
    process.stderr.write(
      `Unlayered font reset check failed: form-control font rules outside @layer defeat Tailwind font utilities (#2861).\n${violations.join('\n')}\n`,
    )
    process.exit(1)
  }
  process.stdout.write(`Unlayered font reset check passed: ${files.length} CSS asset(s), no form-control font rule outside @layer.\n`)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv[2] ?? 'dist/assets')
}
