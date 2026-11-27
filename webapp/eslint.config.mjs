// Style guardrails (see STYLE.md): no color literals in components or code. Colors come from the design tokens,
// read in src/theme/ (which, with src/styles/, is the only place allowed to name them).
import vue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

const COLOR = String.raw`/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|oklch\(/i`
const message = 'Hard-coded color: use a design token (--up-*, via src/theme/charts.ts in code). See STYLE.md.'

export default [
  { ignores: ['dist/**', 'node_modules/**', 'src/theme/**', 'src/styles/**'] },
  ...vue.configs['flat/base'],
  {
    files: ['src/**/*.ts'],
    languageOptions: { parser: tseslint.parser, sourceType: 'module' },
  },
  {
    files: ['src/**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser, extraFileExtensions: ['.vue'], sourceType: 'module' } },
  },
  {
    files: ['src/**/*.{ts,vue}'],
    rules: {
      'no-restricted-syntax': [
        'error',
        { selector: `Literal[value=${COLOR}]`, message },
        { selector: `TemplateElement[value.raw=${COLOR}]`, message },
      ],
      'vue/no-restricted-static-attribute': ['error', { key: 'style', value: COLOR, message }],
    },
  },
]
