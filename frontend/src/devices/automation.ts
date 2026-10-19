import type { Api } from './types'

export type Source = { kind: 'device' | 'outdoor'; device_id?: string | null; metric: string }
export type Condition = { source: Source; test?: 'range' | 'available' | 'unavailable'; lower?: number | null; upper?: number | null }
export type Action = { power: 'on' | 'off' | 'hold'; fan?: number | 'auto' | 'sleep' | null }
export type Window = { days: number[]; start: string; end: string }
export type Rule = { id: string; explanation: string; windows: Window[]; conditions: Condition[]; action: Action }
export type Trigger = { id: string; kind: 'above' | 'below' | 'sustained' | 'not_improving' | 'availability' | 'failures'; source?: Source | null; threshold?: number | null; reset?: number | null; readings: number; window_seconds: number; improvement?: number | null }
export type Ruleset = { version: 1; timezone: string; rules: Rule[]; fallback: Action; consecutive_readings: number; min_change_seconds: number; review_after_seconds: number; reconsideration_cooldown_seconds: number; triggers: Trigger[] }
export type Summary = { decision?: { rule_id: string | null; explanation: string; action: Action }; inhibition: string | null }
export type AutomationSummary = { owner: 'manual' | 'agent'; enabled: boolean; paused_until: number; summary: Summary | null; pending_review: boolean }
export type Step = { at: number; kind: string; name?: string; text?: string; arguments?: string; error?: string }
export type Run = { id: string; status: string; model: string; attempts: number; llm_calls: number; error_kind: string | null; started_at: number; finished_at: number | null; step_count?: number; steps?: Step[] }
export type AutomationState = AutomationSummary & { generation: number; revision: number; pause_seconds: number; timezone: string; ruleset: Ruleset | null; job: { status: string; reasons: { reason: string }[] } | null; run: Run | null }
export type AutomationEvent = { id: number; created_at: number; revision?: number; outcome?: string; inhibition?: string; planning?: string; intent?: unknown; paused_until?: number; decision?: { explanation: string; action?: Action } }
export type Mode = 'auto' | 'custom' | 'off'

export const metricLabels: Record<string, string> = { pm25: 'PM2.5 (µg/m³)', pm1: 'PM1 (µg/m³)', pm10: 'PM10 (µg/m³)', air_quality: 'Air quality (rank 1–4)', temperature: 'Temperature (°C)', humidity: 'Humidity (%)', aqi: 'Overall AQI', pm25_aqi: 'PM2.5 AQI', pm10_aqi: 'PM10 AQI' }
const shortMetrics: Record<string, string> = { pm25: 'PM2.5', pm1: 'PM1', pm10: 'PM10', air_quality: 'Air quality', temperature: 'Temperature', humidity: 'Humidity', aqi: 'Outdoor AQI', pm25_aqi: 'Outdoor PM2.5 AQI', pm10_aqi: 'Outdoor PM10 AQI' }
export const runLabels: Record<string, string> = { running: 'Working on the rules', completed: 'Updated the rules', unchanged: 'Kept the current rules', superseded: 'Stopped after a manual change', failed: 'Could not produce valid rules' }
export const errorLabels: Record<string, string> = { invalid_proposal: 'Could not produce valid rules', timeout: 'Took too long and was stopped', llm_limit: 'Reached the model call limit', model_error: 'The model returned an error', interrupted: 'Stopped when the app restarted', error: 'Stopped by an unexpected error' }
const toolLabels: Record<string, string> = { list_devices: 'Listed devices', get_device: 'Read device status', get_device_history: 'Read reading history', get_outdoor_air_quality: 'Read outdoor air quality', get_outdoor_history: 'Read outdoor history', get_rule_schema: 'Read the rule schema', get_device_automation: 'Read device rules and context', list_rule_events: 'Read recent activity', preview_rules: 'Tested a draft ruleset' }
export const inhibitionLabels: Record<string, string> = { disabled: 'Disabled', paused: 'Paused', waiting_for_rules: 'Waiting for rules', target_unavailable: 'Device unavailable · holding setting', unsupported_action: 'Device capabilities changed', awaiting_confirmation: 'Waiting for confirmation', awaiting_fresh_status: 'Waiting for fresh status', retry_limit: 'Retry limit reached', retry_backoff: 'Retry scheduled', waiting_for_readings: 'Waiting for more readings', minimum_change_interval: 'Waiting between changes', already_applied: 'Setting confirmed', hold: 'Holding setting', manual_pause: 'Paused after manual control' }
const planningLabels: Record<string, string> = { completed: 'Plan revised by Auto', unchanged: 'Auto kept the plan', failed: 'Auto planning failed · will retry' }
const outcomeLabels: Record<string, string> = { controlled: 'Setting sent', unconfirmed: 'Setting not confirmed', control_failed: 'Setting failed', temporary: 'Connection failed', offline: 'Device offline', rate_limit: 'Provider requests paused', auth: 'Account needs attention', invalid_control: 'Setting no longer supported' }
export const triggerLabels = { above: 'Threshold crossed upward', below: 'Threshold crossed downward', sustained: 'Sustained high readings', not_improving: 'High readings without improvement', availability: 'Reading availability changes', failures: 'Repeated command failures' }
export const modeLabels: Record<Mode, string> = { auto: 'Auto', custom: 'Custom rules', off: 'Manual' }
export const modeDescriptions: Record<Mode, string> = { auto: 'Plan maintained by the planner', custom: 'Plan edited by you', off: 'No automation' }
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export function stepText(step: Step): string | null {
  if (step.kind === 'tool') return toolLabels[step.name ?? ''] ?? 'Used ' + step.name
  if (step.kind === 'result') return step.error ? (toolLabels[step.name ?? ''] ?? step.name) + ' failed: ' + step.error : null
  if (step.kind === 'rejected') return 'Rules rejected: ' + step.text
  return step.text ?? null
}

export function runText(run: Run) {
  return run.status === 'failed' ? (errorLabels[run.error_kind ?? ''] ?? runLabels.failed) : runLabels[run.status] ?? run.status
}

export function eventText(event: AutomationEvent) {
  if (event.planning) return planningLabels[event.planning] ?? 'Auto planning'
  if (event.intent && event.decision?.action) return 'Set to ' + actionText(event.decision.action)
  if (event.inhibition === 'manual_pause') return 'Overridden'
  if (event.outcome) return outcomeLabels[event.outcome] ?? 'Setting update'
  if (event.decision?.explanation) return event.decision.explanation
  return event.inhibition ? inhibitionLabels[event.inhibition] ?? 'Automation update' : 'Setting requested'
}

export function eventDetail(event: AutomationEvent) {
  if (event.planning) return event.revision ? `Revision ${event.revision}` : ''
  if (event.intent) return event.decision?.explanation ?? ''
  if (event.inhibition === 'manual_pause' && event.paused_until) return 'Auto paused until ' + new Date(event.paused_until * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
  if (event.outcome && event.decision?.explanation) return event.decision.explanation
  // Without an explanation, eventText already shows the inhibition label.
  if (event.inhibition && event.decision?.explanation) return inhibitionLabels[event.inhibition] ?? ''
  return ''
}

/** Events that record something Auto or a person did, not decision bookkeeping. */
export function isActivity(event: AutomationEvent) {
  return Boolean(event.intent || event.planning || event.inhibition === 'manual_pause' || (event.outcome && event.outcome !== 'controlled'))
}

export function actionText(action: Action) {
  if (action.power !== 'on') return action.power === 'off' ? 'Turn off' : 'Keep current setting'
  return typeof action.fan === 'number' ? 'Level ' + action.fan : action.fan === 'auto' ? 'Built-in auto' : action.fan === 'sleep' ? 'Sleep' : 'Turn on'
}

export function shortActionText(action: Action) {
  return action.power === 'hold' ? 'Keep current' : action.power === 'off' ? 'Off' : actionText(action)
}

function bound(value: number | null | undefined) {
  return value === null || value === undefined ? null : value.toLocaleString()
}

export function conditionText(condition: Condition, deviceName: (id: string | null | undefined) => string | null) {
  const owner = condition.source.kind === 'device' ? deviceName(condition.source.device_id) : null
  const metric = (owner ? owner + ' ' : '') + (shortMetrics[condition.source.metric] ?? condition.source.metric)
  if (condition.test === 'available') return [metric, 'is available']
  if (condition.test === 'unavailable') return [metric, 'is missing']
  const lower = bound(condition.lower), upper = bound(condition.upper)
  if (lower !== null && upper !== null) return [metric, `${lower} to ${upper}`]
  if (lower !== null) return [metric, `≥ ${lower}`]
  if (upper !== null) return [metric, `< ${upper}`]
  return [metric, 'any value']
}

export function windowText(window: Window) {
  const days = window.days.length === 7 ? 'Daily' : window.days.length ? [...window.days].sort().map((day) => DAYS[day]).join(', ') : 'No days'
  return [days, `${window.start}–${window.end}`]
}

export function automationMode(automation: Pick<AutomationSummary, 'owner' | 'enabled'> | null | undefined): Mode {
  if (!automation?.enabled) return 'off'
  return automation.owner === 'agent' ? 'auto' : 'custom'
}

export function isPaused(automation: Pick<AutomationSummary, 'paused_until'> | null | undefined) {
  return (automation?.paused_until ?? 0) > Date.now() / 1000
}

export function automationPath(deviceId: string) {
  return '/api/devices/' + deviceId + '/automation'
}

type Configuration = Pick<AutomationState, 'owner' | 'enabled' | 'pause_seconds'>

/** The PUT /automation body: the current settings with overrides, at the current version. */
export function configuration(state: AutomationState, overrides: Partial<Configuration> = {}) {
  return {
    owner: state.owner, enabled: state.enabled, pause_seconds: state.pause_seconds, ...overrides,
    generation: state.generation, base_revision: state.revision,
  }
}

/** Applies a mode with the current version so concurrent edits are rejected. */
export async function configureMode(api: Api, deviceId: string, mode: Mode, current?: AutomationState): Promise<AutomationState> {
  const state = current ?? await api<AutomationState>(automationPath(deviceId))
  const owner = mode === 'auto' ? 'agent' : mode === 'custom' ? 'manual' : state.owner
  return putConfiguration(api, deviceId, state, { owner, enabled: mode !== 'off' })
}

export function putConfiguration(api: Api, deviceId: string, state: AutomationState, overrides: Partial<Configuration>) {
  return api<AutomationState>(automationPath(deviceId), { method: 'PUT', body: JSON.stringify(configuration(state, overrides)) })
}

export const PAUSE_OPTIONS = [900, 3600, 14400, 86400]
export function durationText(seconds: number) {
  return seconds < 3600 ? `${Math.round(seconds / 60)} min` : `${seconds / 3600} hr`
}
