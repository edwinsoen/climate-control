import { useEffect, useRef, useState } from 'react'
import type { ControlOptions } from './DeviceControls'

type Source = { kind: 'device' | 'outdoor'; device_id?: string | null; metric: string }
type Condition = { source: Source; test?: 'range' | 'available' | 'unavailable'; lower?: number | null; upper?: number | null }
type Action = { power: 'on' | 'off' | 'hold'; fan?: number | 'auto' | 'sleep' | null }
type Window = { days: number[]; start: string; end: string }
type Rule = { id: string; explanation: string; windows: Window[]; conditions: Condition[]; action: Action }
type Trigger = { id: string; kind: 'above' | 'below' | 'sustained' | 'not_improving' | 'availability' | 'failures'; source?: Source | null; threshold?: number | null; reset?: number | null; readings: number; window_seconds: number; improvement?: number | null }
type Ruleset = { version: 1; timezone: string; rules: Rule[]; fallback: Action; consecutive_readings: number; min_change_seconds: number; review_after_seconds: number; reconsideration_cooldown_seconds: number; triggers: Trigger[] }
type Summary = { decision?: { rule_id: string | null; explanation: string; action: Action }; inhibition: string | null }
export type AutomationSummary = { owner: 'manual' | 'agent'; enabled: boolean; paused_until: number; summary: Summary | null; pending_review: boolean }
type State = AutomationSummary & { generation: number; revision: number; pause_seconds: number; timezone: string; ruleset: Ruleset | null; job: { status: string; reasons: { reason: string }[] } | null; run: Run | null }
type Step = { at: number; kind: string; name?: string; text?: string; arguments?: string; error?: string }
type Run = { id: string; status: string; model: string; attempts: number; llm_calls: number; error_kind: string | null; started_at: number; finished_at: number | null; step_count?: number; steps?: Step[] }
type Device = { id: string; name: string; capabilities: string[]; controls: ControlOptions }
type Context = State & { devices: Device[] }
type Event = { id: number; created_at: number; outcome?: string; inhibition?: string; planning?: string; decision?: { explanation: string } }
type Preview = { decision: { explanation: string; action: Action; trace: { rule_id: string; matches: boolean; in_time: boolean }[] }; target_available: boolean; inputs: Record<string, { available: boolean; value: number | null; unit: string; age_seconds: number | null }> }
type Api = <T>(path: string, options?: RequestInit) => Promise<T>

const metricLabels: Record<string, string> = { pm25: 'PM2.5 (µg/m³)', pm1: 'PM1 (µg/m³)', pm10: 'PM10 (µg/m³)', air_quality: 'Air quality (rank 1–4)', aqi: 'Overall AQI', pm25_aqi: 'PM2.5 AQI', pm10_aqi: 'PM10 AQI' }
const runLabels: Record<string, string> = { running: 'Working on the rules', completed: 'Updated the rules', unchanged: 'Kept the current rules', superseded: 'Stopped after a manual change', failed: 'Could not produce valid rules' }
const errorLabels: Record<string, string> = { invalid_proposal: 'Could not produce valid rules', timeout: 'Took too long and was stopped', llm_limit: 'Reached the model call limit', model_error: 'The model returned an error', interrupted: 'Stopped when the app restarted', error: 'Stopped by an unexpected error' }
const toolLabels: Record<string, string> = { list_devices: 'Listed devices', get_device: 'Read device status', get_device_history: 'Read reading history', get_outdoor_air_quality: 'Read outdoor air quality', get_outdoor_history: 'Read outdoor history', get_rule_schema: 'Read the rule schema', get_device_automation: 'Read device rules and context', list_rule_events: 'Read recent activity', preview_rules: 'Tested a draft ruleset' }
function stepText(step: Step): string | null {
  if (step.kind === 'tool') return toolLabels[step.name ?? ''] ?? 'Used ' + step.name
  if (step.kind === 'result') return step.error ? (toolLabels[step.name ?? ''] ?? step.name) + ' failed: ' + step.error : null
  if (step.kind === 'rejected') return 'Rules rejected: ' + step.text
  return step.text ?? null
}
const inhibitionLabels: Record<string, string> = { disabled: 'Disabled', paused: 'Paused', waiting_for_rules: 'Waiting for rules', target_unavailable: 'Device unavailable · holding setting', unsupported_action: 'Device capabilities changed', awaiting_confirmation: 'Waiting for confirmation', awaiting_fresh_status: 'Waiting for fresh status', retry_limit: 'Retry limit reached', retry_backoff: 'Retry scheduled', waiting_for_readings: 'Waiting for more readings', minimum_change_interval: 'Waiting between changes', already_applied: 'Setting confirmed', hold: 'Holding setting', manual_pause: 'Paused after manual control' }
const planningLabels: Record<string, string> = { completed: 'Agent updated rules', unchanged: 'Agent kept rules', failed: 'Agent planning failed · will retry' }
const outcomeLabels: Record<string, string> = { controlled: 'Setting sent', unconfirmed: 'Setting not confirmed', control_failed: 'Setting failed', temporary: 'Connection failed', offline: 'Device offline', rate_limit: 'Provider requests paused', auth: 'Account needs attention', invalid_control: 'Setting no longer supported' }
const triggerLabels = { above: 'Threshold crossed upward', below: 'Threshold crossed downward', sustained: 'Sustained high readings', not_improving: 'High readings without improvement', availability: 'Reading availability changes', failures: 'Repeated command failures' }
const newId = () => crypto.randomUUID()
function defaultSource(device: Device): Source {
  const metric = device.capabilities.find(m => ['pm25', 'pm1', 'pm10', 'air_quality'].includes(m))
  return metric ? { kind: 'device', device_id: device.id, metric } : { kind: 'outdoor', metric: 'aqi' }
}
function newRules(timezone: string): Ruleset {
  return { version: 1, timezone, rules: [], fallback: { power: 'hold' }, consecutive_readings: 2, min_change_seconds: 120, review_after_seconds: 86400, reconsideration_cooldown_seconds: 1800, triggers: [] }
}
function actionText(action: Action) {
  if (action.power !== 'on') return action.power === 'off' ? 'Turn off' : 'Keep current setting'
  return typeof action.fan === 'number' ? 'Turn on · Fan ' + action.fan : 'Turn on · ' + (action.fan === 'auto' ? 'Auto' : 'Sleep')
}
function ActionField({ action, controls, onChange }: { action: Action; controls: ControlOptions; onChange: (a: Action) => void }) {
  return <label>Device setting<select data-testid="rule-action" value={action.power === 'on' ? String(action.fan) : action.power} onChange={e => {
    const value = e.target.value
    onChange(value === 'off' || value === 'hold' ? { power: value } : { power: 'on', fan: value === 'auto' || value === 'sleep' ? value : Number(value) })
  }}>
    <option value="hold">Keep current setting</option>
    {controls.power && <option value="off">Turn off</option>}
    {controls.power && (controls.fan_levels ?? []).map(n => <option key={n} value={n}>Turn on · Fan {n}</option>)}
    {controls.power && ['auto', 'sleep'].filter(m => controls.modes?.includes(m)).map(m => <option key={m} value={m}>Turn on · {m === 'auto' ? 'Auto' : 'Sleep'}</option>)}
  </select></label>
}
function SourceField({ source, devices, onChange }: { source: Source; devices: Device[]; onChange: (s: Source) => void }) {
  const selected = devices.find(d => d.id === source.device_id)
  const metrics = source.kind === 'outdoor' ? ['aqi', 'pm25_aqi', 'pm10_aqi'] : (selected?.capabilities ?? []).filter(m => ['pm25', 'pm1', 'pm10', 'air_quality'].includes(m))
  return <div className="automation-fields">
    <label>Source<select data-testid="rule-source" value={source.kind === 'outdoor' ? 'outdoor' : source.device_id ?? ''} onChange={e => {
      if (e.target.value === 'outdoor') onChange({ kind: 'outdoor', metric: 'aqi' })
      else { const device = devices.find(d => d.id === e.target.value); if (device) onChange(defaultSource(device)) }
    }}>
      {source.kind === 'device' && !selected && <option value={source.device_id ?? ''}>Unavailable device</option>}
      {devices.filter(d => d.capabilities.some(m => ['pm25', 'pm1', 'pm10', 'air_quality'].includes(m))).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
      <option value="outdoor">Outdoor station</option>
    </select></label>
    <label>Measurement<select data-testid="rule-metric" value={source.metric} onChange={e => onChange({ ...source, metric: e.target.value })}>
      {!metrics.includes(source.metric) && <option value={source.metric}>{metricLabels[source.metric]} · unavailable</option>}
      {metrics.map(m => <option key={m} value={m}>{metricLabels[m]}</option>)}
    </select></label>
  </div>
}
function NumberField({ label, value, onChange, min = 0, testId }: { label: string; value: number | null | undefined; onChange: (n: number | null) => void; min?: number; testId?: string }) {
  return <label>{label}<input data-testid={testId ?? 'rule-' + label.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-')} type="number" min={min} step="any" value={value ?? ''} placeholder="No limit" onChange={e => onChange(e.target.value === '' ? null : Number(e.target.value))} /></label>
}

export function AutomationPage({ deviceId, api }: { deviceId: string; api: Api }) {
  const path = '/api/devices/' + deviceId + '/automation'
  const [state, setState] = useState<State | null>(null)
  const [devices, setDevices] = useState<Device[]>([])
  const [draft, setDraft] = useState<Ruleset | null>(null)
  const [saved, setSaved] = useState('')
  const [events, setEvents] = useState<Event[]>([])
  const [steps, setSteps] = useState<Step[]>([])
  const [activityOpen, setActivityOpen] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [outdated, setOutdated] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [json, setJson] = useState('')
  const [pauseSeconds, setPauseSeconds] = useState(3600)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const loadedVersion = useRef<{ revision: number; generation: number } | null>(null)
  const errorRef = useRef<HTMLParagraphElement>(null)
  const dirty = draft !== null && JSON.stringify(draft) !== saved
  const device = devices.find(d => d.id === deviceId)
  const readonly = state?.owner === 'agent'
  const paused = (state?.paused_until ?? 0) > Date.now() / 1000

  function acceptContext(result: Context) {
    loadedVersion.current = { revision: result.revision, generation: result.generation }
    setState(result); setDevices(result.devices)
    setDraft(result.ruleset ?? newRules(result.timezone))
    setSaved(JSON.stringify(result.ruleset ?? newRules(result.timezone)))
    setPauseSeconds(result.pause_seconds); setOutdated(false); setPreview(null)
    setCollapsed(new Set((result.ruleset?.rules ?? []).slice(1).map(rule => rule.id)))
  }
  useEffect(() => {
    let cancelled = false
    setBusy(true)
    api<Context>(path + '/context').then(result => { if (!cancelled) acceptContext(result) })
      .catch(err => { if (!cancelled) setError((err as Error).message) })
      .finally(() => { if (!cancelled) setBusy(false) })
    return () => { cancelled = true }
  }, [path, api])

  useEffect(() => {
    if (!dirty || readonly) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty, readonly])

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  const ownerRef = useRef<State['owner'] | null>(null)
  ownerRef.current = state?.owner ?? null

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const refresh = async () => {
      let pendingReview = false
      if (!document.hidden) {
        void api<Event[]>(path + '/events').then(value => { if (!cancelled) setEvents(value) }).catch(() => {})
        try {
          const value = await api<State>(path)
          if (cancelled) return
          pendingReview = value.owner === 'agent' && ['pending', 'claimed'].includes(value.job?.status ?? '')
          const loaded = loadedVersion.current
          if (loaded && (value.revision > loaded.revision || value.generation > loaded.generation)) {
            // Agent-managed rules are read-only here, so load the agent's revision directly.
            if (value.owner === 'agent' && ownerRef.current === 'agent') {
              const context = await api<Context>(path + '/context')
              if (!cancelled) acceptContext(context)
            } else setOutdated(true)
          }
          setState(current => current && { ...current, summary: value.summary, paused_until: value.paused_until, job: value.job, run: value.run })
        } catch { /* keep the last loaded state */ }
      }
      // Check often while the agent is planning; otherwise every 15 seconds.
      if (!cancelled) timer = window.setTimeout(() => void refresh(), pendingReview ? 3000 : 15000)
    }
    void refresh()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [path, api])

  const run = state?.run ?? null
  const running = run?.status === 'running'

  useEffect(() => {
    if (running) setActivityOpen(true)
  }, [running, run?.id])

  useEffect(() => {
    if (!activityOpen && !running) return
    let cancelled = false
    let timer = 0
    const load = async () => {
      try {
        const [latest] = await api<Run[]>(path + '/runs?limit=1')
        if (!cancelled) setSteps(latest?.steps ?? [])
      } catch { /* keep the steps already shown */ }
      if (!cancelled && running) timer = window.setTimeout(() => void load(), 3000)
    }
    void load()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [path, api, activityOpen, running, run?.id, run?.step_count])

  async function perform(task: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await task() } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  async function write(suffix: string, body?: unknown, method = 'POST') {
    const value = await api<State>(path + suffix, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
    loadedVersion.current = { revision: value.revision, generation: value.generation }
    setState(value)
    return value
  }
  function changeDraft(value: Ruleset) { setDraft(value); setPreview(null) }
  function changeRule(index: number, value: Rule) {
    if (draft) changeDraft({ ...draft, rules: draft.rules.map((r, i) => i === index ? value : r) })
  }
  function moveRule(index: number, direction: number) {
    if (!draft) return
    const rules = [...draft.rules]
    ;[rules[index], rules[index + direction]] = [rules[index + direction], rules[index]]
    changeDraft({ ...draft, rules })
  }
  function configure(owner: 'manual' | 'agent', enabled: boolean) {
    if (!state) return
    void perform(async () => { await write('', { owner, enabled, pause_seconds: pauseSeconds, generation: state.generation, base_revision: state.revision }, 'PUT') })
  }
  async function save() {
    if (!state || !draft) return
    const value = await write('/ruleset', { generation: state.generation, base_revision: state.revision, ruleset: draft, reason: 'Updated rules in the editor' }, 'PUT')
    setDraft(value.ruleset); setSaved(JSON.stringify(value.ruleset)); setNotice('Rules saved.'); setPreview(null)
  }
  function addRule() {
    if (draft) changeDraft({ ...draft, rules: [...draft.rules, { id: newId(), explanation: 'New rule', windows: [], conditions: [], action: { power: 'hold' } }] })
  }
  const pending = state?.job?.status === 'pending' || state?.job?.status === 'claimed'
  return <section className="rules-page" data-testid="rules-page">
    <a className="rules-back" data-testid="rules-back" href="/">← All devices</a>
    <div className="rules-page-heading">
      <div><p className="eyebrow">Device automation</p><h1 data-testid="page-title">{device ? device.name + ' rules' : 'Device rules'}</h1></div>
    </div>
    {error && <p ref={errorRef} tabIndex={-1} className="alert error" data-testid="automation-error" role="alert">{error}</p>}
    {notice && <p className="alert notice" data-testid="automation-notice" role="status">{notice}</p>}
    {!draft && !error && <p role="status">Loading rules…</p>}
    {!draft && error && <button className="button" onClick={() => void perform(async () => acceptContext(await api<Context>(path + '/context')))}>Try again</button>}
    {state && draft && device && <>
      {outdated && <div className="alert notice" role="status">Rules or settings changed elsewhere. Reload before saving.
        <button className="text-button" data-testid="automation-reload" disabled={busy} onClick={() => {
          if (dirty && !window.confirm('Discard unsaved changes and reload?')) return
          void perform(async () => acceptContext(await api<Context>(path + '/context')))
        }}>Reload</button>
      </div>}
      <div className="rules-toolbar">
        <span data-testid="automation-draft-status">{readonly ? 'Agent-managed' : dirty ? 'Unsaved changes' : state.revision ? 'Saved' : 'No saved rules'}</span>
        <div className="automation-buttons">
          <button className="button" data-testid="automation-preview" disabled={busy} onClick={() => void perform(async () => { setPreview(await api<Preview>(path + '/preview', { method: 'POST', body: JSON.stringify(draft) })) })}>Preview rules</button>
          <button className="button primary" data-testid="automation-save" disabled={busy || readonly || outdated || !dirty} onClick={() => void perform(save)}>Save rules</button>
        </div>
      </div>
      <div className="rules-layout">
        <div className="rules-main automation-editor">
          {readonly && <p className="alert notice">Agent-managed. Switch to manual to edit.</p>}
          <fieldset disabled={busy || readonly || outdated} className="automation-form">
            <div className="rules-section-heading"><div><h2>Rules</h2><p className="subtle">First matching rule applies.</p></div><button type="button" className="button small" data-testid="rule-add" onClick={addRule}>+ Add rule</button></div>
            {!draft.rules.length && <div className="rules-empty">
              {readonly && (running || pending)
                ? <p data-testid="automation-working">Agent is preparing the first rules…</p>
                : <p>No rules</p>}
            </div>}
            {draft.rules.map((rule, index) => <fieldset className="automation-rule" key={rule.id} data-testid="automation-rule">
              <legend>Rule {index + 1}</legend>
              <div className="rule-heading"><label>Rule name<input data-testid="rule-explanation" value={rule.explanation} placeholder="High indoor PM2.5" onChange={e => changeRule(index, { ...rule, explanation: e.target.value })} /></label>
                <div className="rule-heading-actions">
                  <button type="button" className="button rule-icon-button" data-testid="rule-up" aria-label={'Move rule ' + (index + 1) + ' up'} title="Move up" disabled={index === 0} onClick={() => moveRule(index, -1)}><svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6" /></svg></button>
                  <button type="button" className="button rule-icon-button" data-testid="rule-down" aria-label={'Move rule ' + (index + 1) + ' down'} title="Move down" disabled={index === draft.rules.length - 1} onClick={() => moveRule(index, 1)}><svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6" /></svg></button>
                  <button type="button" className="button rule-icon-button danger" data-testid="rule-remove" aria-label={'Remove rule ' + (index + 1)} title="Remove rule" onClick={() => changeDraft({ ...draft, rules: draft.rules.filter((_, i) => i !== index) })}><svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7m4-7v7" /></svg></button>
                </div>
              </div>
              <details open={!collapsed.has(rule.id)} className="rule-details">
              <summary data-testid="rule-expand" aria-expanded={!collapsed.has(rule.id)} onClick={event => {
                event.preventDefault()
                setCollapsed(current => {
                  const next = new Set(current)
                  if (next.has(rule.id)) next.delete(rule.id); else next.add(rule.id)
                  return next
                })
              }}>{rule.windows.length ? rule.windows.map(w => w.start + '–' + w.end).join(', ') : 'Any time'} · {rule.conditions.length ? String(rule.conditions.length) + (rule.conditions.length === 1 ? ' reading condition' : ' reading conditions') : 'No reading conditions'} · {actionText(rule.action)}</summary>
              <div>
              <div className="rule-part"><h3>When</h3><div className="rule-part-body">
                {!rule.windows.length && <p className="rule-hint">Always</p>}
                {rule.windows.map((window, wi) => <div className="automation-window" key={wi}>
                  <div className="automation-fields"><label>From<input data-testid="rule-time-start" type="time" value={window.start} onChange={e => changeRule(index, { ...rule, windows: rule.windows.map((w, i) => i === wi ? { ...w, start: e.target.value } : w) })} /></label><label>Until<input data-testid="rule-time-end" type="time" value={window.end} onChange={e => changeRule(index, { ...rule, windows: rule.windows.map((w, i) => i === wi ? { ...w, end: e.target.value } : w) })} /></label></div>
                  <div className="automation-days">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day, di) => <label key={day}><input data-testid={'rule-day-' + di} type="checkbox" checked={window.days.includes(di)} onChange={e => changeRule(index, { ...rule, windows: rule.windows.map((w, i) => i === wi ? { ...w, days: e.target.checked ? [...w.days, di] : w.days.filter(d => d !== di) } : w) })} />{day}</label>)}</div>
                  {window.start > window.end && <p className="rule-hint">Ends next day. Selected days apply to the start.</p>}
                  <button type="button" className="text-button" onClick={() => changeRule(index, { ...rule, windows: rule.windows.filter((_, i) => i !== wi) })}>Remove time window</button>
                </div>)}
                <button type="button" className="text-button" data-testid="rule-add-window" onClick={() => changeRule(index, { ...rule, windows: [...rule.windows, { days: [0, 1, 2, 3, 4, 5, 6], start: '22:00', end: '07:00' }] })}>+ Add time window</button>
              </div></div>
              <div className="rule-part"><h3>If</h3><div className="rule-part-body">
                {!rule.conditions.length && <p className="rule-hint">No reading conditions</p>}
                {rule.conditions.length > 1 && <p className="rule-hint">All conditions must match.</p>}
                {rule.conditions.map((condition, ci) => {
                  const update = (value: Condition) => changeRule(index, { ...rule, conditions: rule.conditions.map((c, i) => i === ci ? value : c) })
                  return <div className="automation-condition" key={ci} data-testid="rule-condition">
                    <SourceField source={condition.source} devices={devices} onChange={source => update({ ...condition, source })} />
                    <label>Condition<select data-testid="rule-condition-test" value={condition.test ?? 'range'} onChange={e => update({ source: condition.source, test: e.target.value as Condition['test'], ...(e.target.value === 'range' ? { lower: 0 } : {}) })}><option value="range">In range</option><option value="available">Available</option><option value="unavailable">Missing or stale</option></select></label>
                    {(!condition.test || condition.test === 'range') && <><div className="automation-fields"><NumberField label="At least" value={condition.lower} onChange={lower => update({ ...condition, lower })} /><NumberField label="Below" value={condition.upper} onChange={upper => update({ ...condition, upper })} /></div><p className="rule-hint">Blank means no limit. Missing or stale readings do not match.</p></>}
                    <button type="button" className="text-button" data-testid="rule-remove-condition" onClick={() => changeRule(index, { ...rule, conditions: rule.conditions.filter((_, i) => i !== ci) })}>Remove condition</button>
                  </div>
                })}
                <button type="button" className="text-button" data-testid="rule-add-condition" onClick={() => changeRule(index, { ...rule, conditions: [...rule.conditions, { source: defaultSource(device), test: 'range', lower: 0 }] })}>+ Add reading condition</button>
              </div></div>
              <div className="rule-part"><h3>Then</h3><div className="rule-part-body"><ActionField action={rule.action} controls={device.controls} onChange={action => changeRule(index, { ...rule, action })} /></div></div>
              </div>
              </details>
            </fieldset>)}
            <fieldset className="rules-panel" data-testid="rule-fallback"><legend>When no rule matches</legend><ActionField action={draft.fallback} controls={device.controls} onChange={fallback => changeDraft({ ...draft, fallback })} /></fieldset>
            <details className="rules-panel"><summary>Timing &amp; location</summary><div className="automation-fields">
              <label>Time zone<input data-testid="rule-timezone" value={draft.timezone} onChange={e => changeDraft({ ...draft, timezone: e.target.value })} /></label>
              <NumberField label="Readings before changing" testId="rule-consecutive-readings" min={1} value={draft.consecutive_readings} onChange={n => changeDraft({ ...draft, consecutive_readings: n ?? 1 })} />
              <NumberField label="Minutes between changes" min={5 / 60} value={draft.min_change_seconds / 60} onChange={n => changeDraft({ ...draft, min_change_seconds: Math.round((n ?? 2) * 60) })} />
            </div></details>
            <details className="rules-panel"><summary>Agent review</summary>
              <p className="rule-hint">Applies only to agent-managed devices.</p>
              <div className="automation-fields"><NumberField label="Review every (hours)" min={0.5} value={draft.review_after_seconds / 3600} onChange={n => changeDraft({ ...draft, review_after_seconds: Math.round((n ?? 24) * 3600) })} /><NumberField label="Wait between requests (minutes)" min={1} value={draft.reconsideration_cooldown_seconds / 60} onChange={n => changeDraft({ ...draft, reconsideration_cooldown_seconds: Math.round((n ?? 30) * 60) })} /></div>
              {draft.triggers.map((trigger, ti) => {
                const update = (value: Trigger) => changeDraft({ ...draft, triggers: draft.triggers.map((t, i) => i === ti ? value : t) })
                return <fieldset className="automation-condition" key={trigger.id} data-testid="rule-trigger"><legend>Condition {ti + 1}</legend>
                  <label>Trigger<select data-testid="rule-trigger-kind" value={trigger.kind} onChange={e => update({ id: trigger.id, kind: e.target.value as Trigger['kind'], ...(e.target.value === 'failures' ? {} : { source: defaultSource(device) }), readings: 3, window_seconds: 1800 })}>{Object.entries(triggerLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  {trigger.source && <SourceField source={trigger.source} devices={devices} onChange={source => update({ ...trigger, source })} />}
                  <div className="automation-fields">
                    {!['availability', 'failures'].includes(trigger.kind) && <NumberField label="Threshold" value={trigger.threshold} onChange={threshold => update({ ...trigger, threshold })} />}
                    {['above', 'below'].includes(trigger.kind) && <NumberField label="Reset threshold" value={trigger.reset} onChange={reset => update({ ...trigger, reset })} />}
                    {['sustained', 'not_improving', 'failures'].includes(trigger.kind) && <NumberField label={trigger.kind === 'failures' ? 'Failures' : 'Readings'} min={2} value={trigger.readings} onChange={n => update({ ...trigger, readings: n ?? 3 })} />}
                    {['sustained', 'not_improving'].includes(trigger.kind) && <NumberField label="Within (minutes)" min={1} value={trigger.window_seconds / 60} onChange={n => update({ ...trigger, window_seconds: Math.round((n ?? 30) * 60) })} />}
                    {trigger.kind === 'not_improving' && <NumberField label="Minimum improvement" value={trigger.improvement} onChange={improvement => update({ ...trigger, improvement })} />}
                  </div>
                  <button type="button" className="text-button" onClick={() => changeDraft({ ...draft, triggers: draft.triggers.filter((_, i) => i !== ti) })}>Remove trigger</button>
                </fieldset>
              })}
              <button type="button" className="text-button" data-testid="rule-add-trigger" onClick={() => changeDraft({ ...draft, triggers: [...draft.triggers, { id: newId(), kind: 'availability', source: defaultSource(device), readings: 3, window_seconds: 1800 }] })}>+ Add trigger</button>
            </details>
          </fieldset>
          <details className="rules-panel"><summary>Import / export JSON</summary><textarea data-testid="automation-json" rows={12} value={json} onChange={e => setJson(e.target.value)} aria-label="Ruleset JSON" />
            <div className="automation-buttons"><button className="button small" data-testid="automation-export" onClick={() => setJson(JSON.stringify(draft, null, 2))}>Export JSON</button><button className="button small" data-testid="automation-import" disabled={busy || readonly || outdated} onClick={() => void perform(async () => {
              const parsed: Ruleset = JSON.parse(json)
              const normalized = { ...newRules(parsed.timezone), ...parsed, rules: (parsed.rules ?? []).map(r => ({ ...r, id: r.id || newId(), windows: r.windows ?? [], conditions: r.conditions ?? [] })), triggers: (parsed.triggers ?? []).map(t => ({ ...t, id: t.id || newId(), readings: t.readings ?? 3, window_seconds: t.window_seconds ?? 1800 })) }
              await api(path + '/preview', { method: 'POST', body: JSON.stringify(normalized) })
              changeDraft(normalized); setNotice('Imported. Save to apply.')
            })}>Validate and import</button></div>
          </details>
        </div>
        <aside className="rules-sidebar automation-editor" aria-label="Automation settings and preview">
          <section className="rules-panel"><h2>Automation</h2><p className="rules-status" data-testid="automation-status">{state.enabled ? readonly ? 'Agent-managed' : 'Manual' : 'Disabled'}</p>
            {paused && <p data-testid="automation-pause-status">Paused until {new Date(state.paused_until * 1000).toLocaleString()}</p>}
            {state.summary?.decision && <p className="subtle">{state.summary.decision.explanation}</p>}
            {state.summary?.inhibition && <p className="rule-hint">{inhibitionLabels[state.summary.inhibition]}</p>}
            <button className="button full" data-testid="automation-enable" disabled={busy || outdated || (!state.revision && !readonly)} onClick={() => configure(state.owner, !state.enabled)}>{state.enabled ? 'Disable rules' : 'Enable rules'}</button>
            {!state.revision && !readonly && <p className="rule-hint">Save rules to enable automation.</p>}
            <div className="automation-buttons"><button className="button small" data-testid="automation-pause" disabled={busy || !state.enabled} onClick={() => void perform(async () => { await write('/pause', { seconds: pauseSeconds }) })}>Pause</button><button className="button small" data-testid="automation-resume" disabled={busy || !paused} onClick={() => void perform(async () => { await write('/resume') })}>Resume</button></div>
            <details><summary>Management</summary>
              <button className="button small" data-testid="automation-ownership" disabled={busy || outdated || (dirty && !readonly)} onClick={() => configure(readonly ? 'manual' : 'agent', state.enabled && (!readonly || state.revision > 0))}>{readonly ? 'Switch to manual' : 'Switch to agent'}</button>
              {dirty && !readonly && <p className="rule-hint">Save edits before switching management.</p>}
              {readonly && <button className="text-button" data-testid="automation-reconsider" disabled={busy} onClick={() => void perform(async () => { await write('/reconsider') })}>Request review</button>}
              <label>Pause after manual control<select data-testid="automation-pause-duration" value={pauseSeconds} onChange={e => setPauseSeconds(Number(e.target.value))}>
                {![900, 3600, 14400, 86400].includes(pauseSeconds) && <option value={pauseSeconds}>{pauseSeconds / 60} minutes</option>}
                {[900, 3600, 14400, 86400].map(s => <option value={s} key={s}>{s === 900 ? '15 minutes' : String(s / 3600) + (s === 3600 ? ' hour' : ' hours')}</option>)}
              </select></label><button className="button small" data-testid="automation-save-pause" disabled={busy || outdated} onClick={() => configure(state.owner, state.enabled)}>Save pause duration</button>
            </details>
            {pending && <p className="rule-hint" data-testid="automation-review-status">Agent review pending</p>}
          </section>
          <section className="rules-panel automation-preview"><h2>Preview</h2>
            {!preview && <p className="rule-hint">Uses current readings. No commands sent.</p>}
            {preview && <div data-testid="automation-preview-result"><p><strong>{actionText(preview.decision.action)}</strong></p><p>{preview.decision.explanation}</p>
              {!preview.target_available && <p>Device unavailable · setting retained</p>}
              <ul>{preview.decision.trace.map(r => <li key={r.rule_id}>{draft.rules.find(rule => rule.id === r.rule_id)?.explanation ?? 'Rule'}: {r.matches ? 'matches' : r.in_time ? 'conditions not met' : 'outside time window'}</li>)}</ul>
              <ul>{Object.entries(preview.inputs).map(([key, input]) => { const [kind, id, metric] = key.split(':'); const name = kind === 'outdoor' ? 'Outdoor station' : devices.find(d => d.id === id)?.name ?? 'Unavailable device'; return <li key={key}>{name} · {metricLabels[metric] ?? metric}: {input.available ? String(input.value) + ' ' + input.unit : 'unavailable'}</li> })}</ul>
            </div>}
          </section>
          <details className="rules-panel agent-activity" data-testid="agent-activity" open={activityOpen}
            onToggle={event => setActivityOpen(event.currentTarget.open)}>
            <summary>{running ? 'What the agent is doing' : 'How the agent worked'}</summary>
            {!run && <p className="rule-hint">The agent has not run for this device yet.</p>}
            {run && <>
              <p data-testid="agent-run-status">{run.status === 'failed' ? (errorLabels[run.error_kind ?? ''] ?? runLabels.failed) : runLabels[run.status] ?? run.status}</p>
              <p className="subtle">{run.model} · {run.llm_calls} model call{run.llm_calls === 1 ? '' : 's'}{run.finished_at ? ' · ' + Math.max(1, Math.round(run.finished_at - run.started_at)) + 's' : ''}</p>
              <ol className="agent-steps" data-testid="agent-steps">
                {steps.map(step => [step, stepText(step)] as const).filter(([, text]) => text)
                  .map(([step, text], index) => <li key={index} className={'agent-step ' + step.kind}>{text}</li>)}
              </ol>
              {running && !steps.length && <p className="rule-hint">Reading this device's data…</p>}
            </>}
          </details>
          <details className="rules-panel"><summary>Recent activity</summary><button className="text-button" onClick={() => void perform(async () => setEvents(await api<Event[]>(path + '/events')))}>Refresh activity</button><ul className="rules-events" data-testid="automation-events">{events.map(event => <li key={event.id}><time>{new Date(event.created_at * 1000).toLocaleString()}</time>{event.planning ? planningLabels[event.planning] ?? 'Agent planning' : event.outcome ? outcomeLabels[event.outcome] ?? 'Setting update' : event.decision?.explanation ?? (event.inhibition ? inhibitionLabels[event.inhibition] : 'Setting requested')}</li>)}</ul></details>
        </aside>
      </div>
    </>}
  </section>
}
