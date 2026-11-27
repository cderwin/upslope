// Style guardrails (see STYLE.md): colors come from the design tokens and the only typeface is Geist. Only
// src/styles/tokens.css writes colors.
const NOT_THE_TOKEN_FONT = /^(?!var\(--up-font\)$).*/

export default {
  ignoreFiles: ['src/styles/tokens.css', 'dist/**'],
  overrides: [
    { files: ['**/*.vue', '**/*.html'], customSyntax: 'postcss-html' },
    { files: ['**/*.scss'], customSyntax: 'postcss-scss' },
  ],
  rules: {
    'color-no-hex': true,
    'color-named': 'never',
    'function-disallowed-list': ['rgb', 'rgba', 'hsl', 'hsla', 'hwb', 'lab', 'lch', 'oklab', 'oklch'],
    'declaration-property-value-disallowed-list': {
      'font-family': [NOT_THE_TOKEN_FONT],
      font: [/monospace/i, /serif/i],
    },
  },
}
