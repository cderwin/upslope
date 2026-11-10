import { selectRun } from './api'
import type { ForecastStatus, ModelStatus, Variable } from './types'

/**
 * Which variable and model the map shows. A variable is a layer id shared across models (the server's
 * `variables`); the viewer picks one, then a model among those that have it.
 */

export interface Selection {
  model: string
  variable: string
}

/** The layer ids each model's loaded manifest has frames for; a model not in it isn't loaded yet. */
export type LoadedLayers = Record<string, ReadonlySet<string>>

export interface Choices {
  status: ForecastStatus
  /** The run to show (see `selectRun`). */
  runPath: string | null
  loaded: LoadedLayers
  /** The viewer's default model, from their settings. */
  preferred: string | null
}

/**
 * Whether the map can show `variable` from `model`: it's configured, the model has the run, and the run has
 * frames of it (assumed until its manifest is loaded).
 */
export function hasVariable(choices: Choices, model: ModelStatus, variable: string): boolean {
  if (!model.layers.includes(variable) || selectRun(model, choices.runPath) === null) return false
  return choices.loaded[model.id]?.has(variable) ?? true
}

/** The model to show `variable` from: `first` if it has it, else the viewer's default, the config's, or any. */
export function pickModel(choices: Choices, variable: string, first?: string | null): ModelStatus | undefined {
  const { status } = choices
  const order = [first, choices.preferred, status.default_model, ...status.models.map((model) => model.id)]
  return order
    .map((id) => status.models.find((model) => model.id === id))
    .find((model) => model !== undefined && hasVariable(choices, model, variable))
}

/**
 * The selection for a requested model and variable (from the URL; either may be missing or unavailable). A
 * variable the requested model lacks comes from another model; a missing or unavailable variable is replaced
 * by the first one the requested (or default) model has. Null if nothing can be shown.
 */
export function resolveSelection(choices: Choices, model?: string, variable?: string): Selection | null {
  const { status } = choices
  const available = (id: string) => status.models.some((m) => hasVariable(choices, m, id))
  let chosen = variable !== undefined && available(variable) ? variable : undefined
  if (chosen === undefined) {
    const start = [model, choices.preferred, status.default_model]
      .map((id) => status.models.find((m) => m.id === id))
      .find((m) => m !== undefined && selectRun(m, choices.runPath) !== null)
    const ids = status.variables.map((v) => v.id)
    chosen = ids.find((id) => start !== undefined && hasVariable(choices, start, id)) ?? ids.find(available)
  }
  if (chosen === undefined) return null
  const picked = pickModel(choices, chosen, model)
  return picked ? { model: picked.id, variable: chosen } : null
}

export interface VariableGroup {
  /** Null for variables without a group. */
  name: string | null
  variables: (Variable & { disabled: boolean })[]
}

/** The variables in display order, under their headings, marked disabled where no model can show them. */
export function variableGroups(choices: Choices): VariableGroup[] {
  const groups: VariableGroup[] = []
  for (const variable of choices.status.variables) {
    const disabled = !choices.status.models.some((model) => hasVariable(choices, model, variable.id))
    let group = groups.find((g) => g.name === variable.group)
    if (!group) groups.push((group = { name: variable.group, variables: [] }))
    group.variables.push({ ...variable, disabled })
  }
  return groups
}

export interface ModelChoice {
  id: string
  name: string
  /** The run's initialization time, if the model has the run. */
  initialized: string | null
  disabled: boolean
}

/** The models configured with `variable`, in config order, marked disabled where they can't show it. */
export function modelChoices(choices: Choices, variable: string): ModelChoice[] {
  return choices.status.models
    .filter((model) => model.layers.includes(variable))
    .map((model) => ({
      id: model.id,
      name: model.name,
      initialized: selectRun(model, choices.runPath)?.initialized ?? null,
      disabled: !hasVariable(choices, model, variable),
    }))
}

/** The hour of a run (initialized at `to`) valid at the same time as `hour` of a run initialized at `from`. */
export function sameValidHour(from: string, hour: number, to: string): number {
  return hour + (new Date(from).getTime() - new Date(to).getTime()) / 3_600_000
}
