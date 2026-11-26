<script lang="ts">
/** The chart the pointer is over, shared by every chart (module scope, unlike `<script setup>`'s). */
let hovered: HTMLElement | null = null
</script>

<script setup lang="ts">
import { LineChart, ScatterChart } from 'echarts/charts'
import { GridComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import type { EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

// Only the parts the point forecast uses, so the dialog's chunk stays small.
echarts.use([LineChart, ScatterChart, GridComponent, TooltipComponent, MarkAreaComponent, MarkLineComponent, CanvasRenderer])

/** One ECharts chart. Charts in the same `group` share the hover: a crosshair at one time shows on all of them. */
const props = defineProps<{
  option: EChartsCoreOption
  group: string
  /** A model to emphasize (its series id), e.g. while its control is hovered; null for none. */
  highlight: string | null
}>()

const element = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resize: ResizeObserver | undefined

/**
 * The linked charts all move their crosshair together, but only the one under the pointer shows its tooltip:
 * every chart's tooltip at once would cover the page. The others' tooltips render empty, which ECharts hides.
 */
function withLocalTooltip(option: EChartsCoreOption): EChartsCoreOption {
  const tooltip = option.tooltip as { formatter?: (params: unknown) => string } | undefined
  const formatter = tooltip?.formatter
  if (!tooltip || !formatter) return option
  return {
    ...option,
    tooltip: { ...tooltip, formatter: (params: unknown) => (hovered === element.value ? formatter(params) : '') },
  }
}

onMounted(() => {
  if (!element.value) return
  chart = echarts.init(element.value, null, { renderer: 'canvas' })
  chart.group = props.group
  echarts.connect(props.group)
  chart.setOption(withLocalTooltip(props.option))
  element.value.addEventListener('pointerenter', onEnter)
  resize = new ResizeObserver(() => chart?.resize())
  resize.observe(element.value)
})

function onEnter() {
  hovered = element.value
}

onBeforeUnmount(() => {
  element.value?.removeEventListener('pointerenter', onEnter)
  if (hovered === element.value) hovered = null
  resize?.disconnect()
  chart?.dispose()
  chart = undefined
})

// A new option replaces the old one (models, window, units or theme changed), rather than merging into it.
watch(
  () => props.option,
  (option) => chart?.setOption(withLocalTooltip(option), { notMerge: true }),
)

watch(
  () => props.highlight,
  (model, previous) => {
    if (!chart) return
    if (previous) chart.dispatchAction({ type: 'downplay', seriesId: previous })
    if (model) chart.dispatchAction({ type: 'highlight', seriesId: model })
  },
)
</script>

<template>
  <div ref="element" class="forecast-chart"></div>
</template>

<style scoped>
.forecast-chart {
  width: 100%;
  height: 170px;
}
</style>
