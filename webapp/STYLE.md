# Upslope style

The webapp's look comes from one set of design tokens. This is the reference for
using them; `src/styles/tokens.css` holds the values.

## Tokens (`src/styles/tokens.css`)

Every color is a CSS custom property, `--up-*`, written in OKLCH (so tokens at the
same lightness look equally heavy), with values for both themes:
`[data-bs-theme="dark"]` (the default) and `[data-bs-theme="light"]`.

| Group | Tokens | Use |
| --- | --- | --- |
| Surfaces | `--up-bg`, `--up-nav-bg`, `--up-surface`, `--up-raised`, `--up-line` | Page, navbar, panels/cards/popovers, inputs/chips/hovered rows, borders/gridlines |
| Text | `--up-ink`, `--up-muted`, `--up-link`, `--up-on-accent` | Primary text, secondary text and axis labels, links, text on accent fills |
| Accent | `--up-accent` | Primary buttons, active states, focus |
| Navbar | `--up-nav-ink`, `--up-nav-muted`, `--up-nav-link` | The navbar stays dark navy in the light theme; in dark these alias ink/muted/link |
| Charts | `--up-series-1` … `--up-series-10`, `--up-night` | One series color per model; night shading |
| Status | `--up-{good,watch,warning,danger}` and `-bg` / `-fg` | Dots, icons and lines; tinted chip fill and its text |
| Imagery | `--up-map-extent`, `--up-overlay-*` | Drawn over the satellite map and annotation images, so the same in both themes |

Bootstrap is themed from these in `src/styles/bootstrap-theme.scss`: its runtime
`--bs-*` variables, buttons, forms, navbar, modals, dropdowns, tables and the
contextual classes (`text-bg-success` → good, `warning` → warning, `danger` →
danger, `info` → accent). Use stock Bootstrap components and classes; style a
component yourself only when Bootstrap has nothing for it.

Need a new color? Add a token to `tokens.css`, for both themes, and list it here
first.

## Code (`src/theme/`)

- `useTheme()`: the theme (`dark` | `light`), `setTheme`, `toggle`. It starts from
  `localStorage.upslopeTheme`, else the OS preference, else dark; the navbar's
  sun/moon and Settings change it.
- `charts.ts`: `modelColor(model)` (a model's `--up-series-N`, keyed by model id,
  never by index), `modelColorVar(model)` for the DOM, `chartChrome()` /
  `useChartChrome()` (gridlines, ticks, night, now, tooltip, font), and
  `statusColor(level)`. Libraries that can't parse OKLCH (ECharts, Mapbox) get
  colors through `token()` / `toColor()`, which convert in the browser. Charts
  re-read colors when the theme changes.

## Map colormaps

The exporter colors map frames and writes their legends into manifests
(`python/upslope/models/dynamical/scales.py`); the webapp only draws the legends
it's given. Temperature (a continuous ramp, sampled every 2 °F, legend drawn as a
gradient) and snowfall (discrete bins, extended past 18 in to 24, 36 and 48+) use
the design ramps, written in OKLCH there. Images that bring their own colors (UW
WRF) are left alone.

## Typography

One family, Geist (`var(--up-font)`, self-hosted from `@fontsource/geist`, weights
400–700). No monospace anywhere; numbers that line up use `.tnum`
(`font-variant-numeric: tabular-nums`).

| Role | Size / weight |
| --- | --- |
| Wordmark | 19px / 600, letter-spacing -0.01em |
| Page or modal title | 24px / 600, letter-spacing -0.01em |
| Chart title | 13px / 600; its unit 400, `--up-muted` |
| Body and controls | 14px / 400–500 |
| Chips, segmented controls | 12px / 500 |
| Meta (coordinates, elevation) | 13px / 400, `--up-muted`, tabular |
| Axis and legend ticks | 10–11px / 400, `--up-muted`, tabular |
| Overlines (`.overline`) | 11px / 500, uppercase, letter-spacing 0.06em, `--up-muted` |

## Guardrails

`just web-lint` (and CI) runs ESLint (no color literals in `.ts`/`.vue` outside
`src/theme/` and `src/styles/`) and Stylelint (no hex, named colors or color
functions, and no `font-family` but `var(--up-font)`, outside `tokens.css`).
