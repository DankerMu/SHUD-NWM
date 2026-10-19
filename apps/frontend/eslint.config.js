import { defineConfig } from 'eslint/config'
import tseslint from 'typescript-eslint'
import importX from 'eslint-plugin-import-x'
import { createTypeScriptImportResolver } from 'eslint-import-resolver-typescript'

export default defineConfig([
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: { parser: tseslint.parser },
    plugins: { '@typescript-eslint': tseslint.plugin, 'import-x': importX },
    settings: {
      // Without this, import-x only follows .js dependencies, so
      // import-x/no-cycle never walks the .ts/.tsx graph and passes vacuously.
      'import-x/extensions': ['.ts', '.tsx'],
      'import-x/resolver-next': [
        createTypeScriptImportResolver({ project: `${import.meta.dirname}/tsconfig.app.json` }),
      ],
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      'import-x/no-cycle': 'error',
    },
  },
  {
    files: ['src/{stores,components,lib,api,hooks}/**/*.{ts,tsx}'],
    ignores: ['**/__tests__/**', '**/*.test.*'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/pages', '@/pages/**', '**/pages', '**/pages/**'],
              message: 'Lower layers (stores/components/lib/api/hooks) must not import from src/pages.',
            },
          ],
        },
      ],
    },
  },
])
