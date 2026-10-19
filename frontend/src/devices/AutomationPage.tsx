import { useEffect, useRef, useState } from 'react'
import { Icon } from '../components/Icon'
import {
  actionText, automationMode, automationPath, conditionText, configuration, metricLabels, shortActionText, triggerLabels, windowText,
  type Action, type AutomationState, type Condition, type Rule, type Ruleset, type Source, type Trigger,
} from './automation'
import type { ControlOptions } from './DeviceControls'
import type { Api } from './types'

type Device = { id: string; name: string; capabilities: string[]; controls: ControlOptions }
type Context = AutomationState & { devices: Device[] }
type Preview = { decision: { rule_id?: string | null; explanation: string; action: Action; trace: { rule_id: string; matches: boolean; in_time: boolean }[] }; target_available: boolean; inputs: Record<string, { available: boolean; value: number | null; unit: string; age_seconds: number | null }> }

const INDOOR_METRICS = ['pm25', 'pm1', 'pm10', 'air_quality', 'temperature', 'humidity']
const newId = () => crypto.randomUUID()
function newRules(timezone: string): Ruleset {
  return { version: 1, timezone, rules: [], fallback: { power: 'hold' }, consecutive_readings: 2, min_change_seconds: 120, review_after_seconds: 86400, reconsideration_cooldown_seconds: 1800, triggers: [] }
}
function defaultSource(device: Device): Source {
  const metric = device.capabilities.find(m => INDOOR_METRICS.includes(m))
  return metric ? { kind: 'device', device_id: device.id, metric } : { kind: 'outdoor', metric: 'aqi' }
}
function ActionField({ action, controls, onChange }: { action: Action; controls: ControlOptions; onChange: (a: Action) => void }) {
  return <label><span className="visually-hidden">Device setting</span><select data-testid="rule-action" value={action.power === 'on' ? String(action.fan) : action.power} onChange={e => {
    const value = e.target.value
    onChange(value === 'off' || value === 'hold' ? { power: value } : { power: 'on', fan: value === 'auto' || value === 'sleep' ? value : Number(value) })
  }}>
    <option value="hold">Keep current setting</option>
    {controls.power && <option value="off">Turn off</option>}
    {controls.power && (controls.fan_levels ?? []).map(n => <option key={n} value={n}>Turn on · Level {n}</option>)}
    {controls.power && ['auto', 'sleep'].filter(m => controls.modes?.includes(m)).map(m => <option key={m} value={m}>Turn on · {m === 'auto' ? 'Built-in auto' : 'Sleep'}</option>)}
  </select></label>
}
function SourceField({ source, devices, onChange }: { source: Source; devices: Device[]; onChange: (s: Source) => void }) {
  const selected = devices.find(d => d.id === source.device_id)
  const metrics = source.kind === 'outdoor' ? ['aqi', 'pm25_aqi', 'pm10_aqi'] : (selected?.capabilities ?? []).filter(m => INDOOR_METRICS.includes(m))
  return <div className="automation-fields">
    <label>Source<select data-testid="rule-source" value={source.kind === 'outdoor' ? 'outdoor' : source.device_id ?? ''} onChange={e => {
      if (e.target.value === 'outdoor') onChange({ kind: 'outdoor', metric: 'aqi' })
      else { const device = devices.find(d => d.id === e.target.value); if (device) onChange(defaultSource(device)) }
    }}>
      {source.kind === 'device' && !selected && <option value={source.device_id ?? ''}>Unavailable device</option>}
      {devices.filter(d => d.capabilities.some(m => INDOOR_METRICS.includes(m))).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
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
  const path = automationPath(deviceId)
  const [state, setState] = useState<AutomationState | null>(null)
  const [devices, setDevices] = useState<Device[]>([])
  const [draft, setDraft] = useState<Ruleset | null>(null)
  const [saved, setSaved] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [outdated, setOutdated] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [json, setJson] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const loadedVersion = useRef<{ revision: number; generation: number } | null>(null)
  const errorRef = useRef<HTMLParagraphElement>(null)
  const dirty = draft !== null && JSON.stringify(draft) !== saved
  const dirtyRef = useRef(false)
  dirtyRef.current = dirty
  const device = devices.find(d => d.id === deviceId)
  const mode = automationMode(state)

  function acceptContext(result: Context) {
    loadedVersion.current = { revision: result.revision, generation: result.generation }
    setState(result); setDevices(result.devices)
    setDraft(result.ruleset ?? newRules(result.timezone))
    setSaved(JSON.stringify(result.ruleset ?? newRules(result.timezone)))
    setOutdated(false); setPreview(null)
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
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const refresh = async () => {
      let pendingReview = false
      if (!document.hidden) {
        try {
          const value = await api<AutomationState>(path)
          if (cancelled) return
          pendingReview = value.owner === 'agent' && ['pending', 'claimed'].includes(value.job?.status ?? '')
          const loaded = loadedVersion.current
          if (loaded && (value.revision > loaded.revision || value.generation > loaded.generation)) {
            // A new Auto revision replaces an untouched draft; edits are kept until the person reloads.
            if (value.owner === 'agent' && !dirtyRef.current) {
              const context = await api<Context>(path + '/context')
              if (!cancelled) acceptContext(context)
            } else setOutdated(true)
          }
          setState(current => current && { ...current, summary: value.summary, paused_until: value.paused_until, job: value.job, run: value.run })
        } catch { /* keep the last loaded state */ }
      }
      // Check often while Auto is planning; otherwise every 15 seconds.
      if (!cancelled) timer = window.setTimeout(() => void refresh(), pendingReview ? 3000 : 15000)
    }
    void refresh()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [path, api])

  async function perform(task: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await task() } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  async function write(suffix: string, body: unknown, method: string) {
    const value = await api<AutomationState>(path + suffix, { method, body: JSON.stringify(body) })
    loadedVersion.current = { revision: value.revision, generation: value.generation }
    setState(value)
    return value
  }
  const configure = (current: AutomationState, owner: 'manual' | 'agent', enabled: boolean) =>
    write('', configuration(current, { owner, enabled }), 'PUT')
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
  // Saving a plan always leaves the device on Custom rules, including a plan Auto wrote.
  async function save() {
    if (!state || !draft) return
    // Validate before changing ownership so an invalid draft leaves the mode untouched.
    await api(path + '/preview', { method: 'POST', body: JSON.stringify(draft) })
    const original = state
    let current = state
    if (current.owner === 'agent') current = await configure(current, 'manual', false)
    try {
      current = await write('/ruleset', { generation: current.generation, base_revision: current.revision, ruleset: draft, reason: 'Updated rules in the editor' }, 'PUT')
    } catch (err) {
      if (current !== original) await configure(current, original.owner, original.enabled).catch(() => {})
      throw err
    }
    if (!current.enabled) current = await configure(current, 'manual', true)
    setDraft(current.ruleset); setSaved(JSON.stringify(current.ruleset)); setNotice('Plan saved.'); setPreview(null)
  }
  function addRule() {
    if (draft) changeDraft({ ...draft, rules: [...draft.rules, { id: newId(), explanation: 'New rule', windows: [], conditions: [], action: { power: 'hold' } }] })
  }
  const pending = state?.owner === 'agent' && ['pending', 'claimed'].includes(state.job?.status ?? '')
  const running = state?.run?.status === 'running'
  const deviceName = (id: string | null | undefined) => !id || id === deviceId ? null : devices.find(d => d.id === id)?.name ?? 'Unavailable purifier'
  const ruleLabel = (id: string | null | undefined) => {
    const index = draft?.rules.findIndex(rule => rule.id === id) ?? -1
    return index >= 0 ? `${index + 1} ${draft!.rules[index].explanation}` : 'Fallback'
  }
  const status = dirty ? 'Unsaved changes' : !state?.revision ? 'No saved plan' : state.owner === 'agent' ? 'Written by Auto' : 'Saved'
  return <main className="page detail" data-testid="rules-page">
    <div className="page-header">
      <div>
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <a href="/">Home</a><span aria-hidden="true">/</span>
          <a href={'/devices/' + deviceId} data-testid="rules-back">{device?.name ?? 'Purifier'}</a><span aria-hidden="true">/</span>
          <span aria-current="page">Plan</span>
        </nav>
        <div className="title-row">
          <h1 data-testid="page-title">{device ? 'Edit plan' : 'Plan'}</h1>
          <span className={'badge' + (dirty ? ' warning' : '')} data-testid="automation-draft-status">{status}</span>
        </div>
      </div>
      {draft && device && <div className="actions">
        <button type="button" className="button" data-testid="automation-discard" disabled={busy || !dirty}
          onClick={() => { changeDraft(JSON.parse(saved)); setNotice('') }}>Discard</button>
        <button type="button" className="button primary" data-testid="automation-save" disabled={busy || outdated || !dirty}
          onClick={() => void perform(save)}>{mode === 'custom' ? 'Save plan' : 'Save as custom plan'}</button>
      </div>}
    </div>
    {error && <p ref={errorRef} tabIndex={-1} className="alert error" data-testid="automation-error" role="alert">{error}</p>}
    {notice && <p className="alert notice" data-testid="automation-notice" role="status">{notice}</p>}
    {!draft && !error && <p role="status" className="subtle">Loading plan…</p>}
    {!draft && error && <button className="button" onClick={() => void perform(async () => acceptContext(await api<Context>(path + '/context')))}>Try again</button>}
    {state && draft && device && <>
      {outdated && <div className="alert notice" role="status">The plan or settings changed elsewhere. Reload before saving.{' '}
        <button className="text-button" data-testid="automation-reload" disabled={busy} onClick={() => {
          if (dirty && !window.confirm('Discard unsaved changes and reload?')) return
          void perform(async () => acceptContext(await api<Context>(path + '/context')))
        }}>Reload</button>
      </div>}
      {mode !== 'custom' && <div className="alert neutral" role="note">
        {state.owner === 'agent' ? 'Auto wrote this plan. ' : ''}Saving switches this device to <strong>Custom rules</strong>.
      </div>}
      <div className="layout">
        <div className="layout-main automation-editor">
          <fieldset disabled={busy || outdated} className="automation-form">
            <div className="section-row"><h2>Rules</h2><span className="hint">First match wins</span></div>
            {!draft.rules.length && <div className="rules-empty">
              {state.owner === 'agent' && (running || pending)
                ? <p data-testid="automation-working">Auto is preparing the first plan…</p>
                : <p>No rules yet. Add one, or rely on the fallback.</p>}
            </div>}
            {draft.rules.map((rule, index) => <fieldset className="automation-rule" key={rule.id} data-testid="automation-rule" aria-label={'Rule ' + (index + 1)}>
              <div className="rule-heading">
                <span className="plan-index">{index + 1}</span>
                <label className="rule-name"><span className="visually-hidden">Rule name</span><input data-testid="rule-explanation" value={rule.explanation} placeholder="High indoor PM2.5" onChange={e => changeRule(index, { ...rule, explanation: e.target.value })} /></label>
                <div className="rule-heading-actions">
                  <button type="button" className="icon-button bare" data-testid="rule-up" aria-label={'Move rule ' + (index + 1) + ' up'} title="Move up" disabled={index === 0} onClick={() => moveRule(index, -1)}><Icon name="up" /></button>
                  <button type="button" className="icon-button bare" data-testid="rule-down" aria-label={'Move rule ' + (index + 1) + ' down'} title="Move down" disabled={index === draft.rules.length - 1} onClick={() => moveRule(index, 1)}><Icon name="down" /></button>
                  <button type="button" className="icon-button bare" data-testid="rule-remove" aria-label={'Delete rule ' + (index + 1)} title="Delete rule" onClick={() => changeDraft({ ...draft, rules: draft.rules.filter((_, i) => i !== index) })}><Icon name="trash" /></button>
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
              }}>
                {!rule.windows.length && !rule.conditions.length ? 'Always' : [
                  ...rule.windows.map(window => windowText(window).join(' ')),
                  ...rule.conditions.map(condition => conditionText(condition, deviceName).join(' ')),
                ].join(' · ')} → {shortActionText(rule.action)}
              </summary>
              <div className="rule-body">
              <div className="rule-part"><h3>When</h3><div className="rule-part-body">
                {!rule.windows.length && <p className="rule-hint">Any time</p>}
                {rule.windows.map((window, wi) => <div className="automation-window" key={wi}>
                  <div className="automation-fields"><label>From<input data-testid="rule-time-start" type="time" value={window.start} onChange={e => changeRule(index, { ...rule, windows: rule.windows.map((w, i) => i === wi ? { ...w, start: e.target.value } : w) })} /></label><label>Until<input data-testid="rule-time-end" type="time" value={window.end} onChange={e => changeRule(index, { ...rule, windows: rule.windows.map((w, i) => i === wi ? { ...w, end: e.target.value } : w) })} /></label></div>
                  <div className="automation-days">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day, di) => <label key={day}><input data-testid={'rule-day-' + di} type="checkbox" checked={window.days.includes(di)} onChange={e => changeRule(index, { ...rule, windows: rule.windows.map((w, i) => i === wi ? { ...w, days: e.target.checked ? [...w.days, di] : w.days.filter(d => d !== di) } : w) })} />{day}</label>)}</div>
                  {window.start > window.end && <p className="rule-hint">Ends next day. Selected days apply to the start.</p>}
                  <button type="button" className="text-button danger" onClick={() => changeRule(index, { ...rule, windows: rule.windows.filter((_, i) => i !== wi) })}>Remove time window</button>
                </div>)}
                <button type="button" className="text-button" data-testid="rule-add-window" onClick={() => changeRule(index, { ...rule, windows: [...rule.windows, { days: [0, 1, 2, 3, 4, 5, 6], start: '22:00', end: '07:00' }] })}>+ Add time window</button>
              </div></div>
              <div className="rule-part"><h3>If</h3><div className="rule-part-body">
                {!rule.conditions.length && <p className="rule-hint">No reading conditions</p>}
                {rule.conditions.length > 1 && <p className="rule-hint">All conditions must match.</p>}
                {rule.conditions.map((condition, ci) => {
                  const update = (value: Condition) => changeRule(index, { ...rule, conditions: rule.conditions.map((c, i) => i === ci ? value : c) })
                  const condMin = condition.source.metric === 'temperature' ? -50 : 0
                  return <div className="automation-condition" key={ci} data-testid="rule-condition">
                    <SourceField source={condition.source} devices={devices} onChange={source => update({ ...condition, source })} />
                    <label>Condition<select data-testid="rule-condition-test" value={condition.test ?? 'range'} onChange={e => update({ source: condition.source, test: e.target.value as Condition['test'], ...(e.target.value === 'range' ? { lower: 0 } : {}) })}><option value="range">In range</option><option value="available">Available</option><option value="unavailable">Missing or stale</option></select></label>
                    {(!condition.test || condition.test === 'range') && <><div className="automation-fields"><NumberField label="At least" min={condMin} value={condition.lower} onChange={lower => update({ ...condition, lower })} /><NumberField label="Below" min={condMin} value={condition.upper} onChange={upper => update({ ...condition, upper })} /></div><p className="rule-hint">Blank means no limit. Missing or stale readings do not match.</p></>}
                    <button type="button" className="text-button danger" data-testid="rule-remove-condition" onClick={() => changeRule(index, { ...rule, conditions: rule.conditions.filter((_, i) => i !== ci) })}>Remove condition</button>
                  </div>
                })}
                <button type="button" className="text-button" data-testid="rule-add-condition" onClick={() => changeRule(index, { ...rule, conditions: [...rule.conditions, { source: defaultSource(device), test: 'range', lower: 0 }] })}>+ Add condition</button>
              </div></div>
              <div className="rule-part"><h3>Then</h3><div className="rule-part-body"><ActionField action={rule.action} controls={device.controls} onChange={action => changeRule(index, { ...rule, action })} /></div></div>
              </div>
              </details>
            </fieldset>)}
            <button type="button" className="button dashed" data-testid="rule-add" onClick={addRule}>+ Add rule</button>
            <fieldset className="rules-panel inline" data-testid="rule-fallback" aria-labelledby="fallback-title"><h3 id="fallback-title">If no rule matches</h3><ActionField action={draft.fallback} controls={device.controls} onChange={fallback => changeDraft({ ...draft, fallback })} /></fieldset>
            <section className="rules-panel" aria-labelledby="timing-title"><h3 id="timing-title">Timing</h3><div className="automation-fields">
              <NumberField label="Readings before changing" testId="rule-consecutive-readings" min={1} value={draft.consecutive_readings} onChange={n => changeDraft({ ...draft, consecutive_readings: n ?? 1 })} />
              <NumberField label="Minutes between changes" min={5 / 60} value={draft.min_change_seconds / 60} onChange={n => changeDraft({ ...draft, min_change_seconds: Math.round((n ?? 2) * 60) })} />
              <label>Time zone<input data-testid="rule-timezone" value={draft.timezone} onChange={e => changeDraft({ ...draft, timezone: e.target.value })} /></label>
            </div></section>
            <details className="rules-panel"><summary>Auto review</summary>
              <p className="rule-hint">Applies when Auto maintains this plan.</p>
              <div className="automation-fields"><NumberField label="Review every (hours)" min={0.5} value={draft.review_after_seconds / 3600} onChange={n => changeDraft({ ...draft, review_after_seconds: Math.round((n ?? 24) * 3600) })} /><NumberField label="Wait between requests (minutes)" min={1} value={draft.reconsideration_cooldown_seconds / 60} onChange={n => changeDraft({ ...draft, reconsideration_cooldown_seconds: Math.round((n ?? 30) * 60) })} /></div>
              {draft.triggers.map((trigger, ti) => {
                const update = (value: Trigger) => changeDraft({ ...draft, triggers: draft.triggers.map((t, i) => i === ti ? value : t) })
                const triggerMin = trigger.source?.metric === 'temperature' ? -50 : 0
                return <fieldset className="automation-condition" key={trigger.id} data-testid="rule-trigger"><legend>Trigger {ti + 1}</legend>
                  <label>Trigger<select data-testid="rule-trigger-kind" value={trigger.kind} onChange={e => update({ id: trigger.id, kind: e.target.value as Trigger['kind'], ...(e.target.value === 'failures' ? {} : { source: defaultSource(device) }), readings: 3, window_seconds: 1800 })}>{Object.entries(triggerLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  {trigger.source && <SourceField source={trigger.source} devices={devices} onChange={source => update({ ...trigger, source })} />}
                  <div className="automation-fields">
                    {!['availability', 'failures'].includes(trigger.kind) && <NumberField label="Threshold" min={triggerMin} value={trigger.threshold} onChange={threshold => update({ ...trigger, threshold })} />}
                    {['above', 'below'].includes(trigger.kind) && <NumberField label="Reset threshold" min={triggerMin} value={trigger.reset} onChange={reset => update({ ...trigger, reset })} />}
                    {['sustained', 'not_improving', 'failures'].includes(trigger.kind) && <NumberField label={trigger.kind === 'failures' ? 'Failures' : 'Readings'} min={2} value={trigger.readings} onChange={n => update({ ...trigger, readings: n ?? 3 })} />}
                    {['sustained', 'not_improving'].includes(trigger.kind) && <NumberField label="Within (minutes)" min={1} value={trigger.window_seconds / 60} onChange={n => update({ ...trigger, window_seconds: Math.round((n ?? 30) * 60) })} />}
                    {trigger.kind === 'not_improving' && <NumberField label="Minimum improvement" value={trigger.improvement} onChange={improvement => update({ ...trigger, improvement })} />}
                  </div>
                  <button type="button" className="text-button danger" onClick={() => changeDraft({ ...draft, triggers: draft.triggers.filter((_, i) => i !== ti) })}>Remove trigger</button>
                </fieldset>
              })}
              <button type="button" className="text-button" data-testid="rule-add-trigger" onClick={() => changeDraft({ ...draft, triggers: [...draft.triggers, { id: newId(), kind: 'availability', source: defaultSource(device), readings: 3, window_seconds: 1800 }] })}>+ Add trigger</button>
            </details>
          </fieldset>
          <details className="rules-panel"><summary>Import or export JSON</summary><textarea data-testid="automation-json" rows={12} value={json} onChange={e => setJson(e.target.value)} aria-label="Ruleset JSON" />
            <div className="actions"><button className="button small" data-testid="automation-export" onClick={() => setJson(JSON.stringify(draft, null, 2))}>Export JSON</button><button className="button small" data-testid="automation-import" disabled={busy || outdated} onClick={() => void perform(async () => {
              const parsed: Ruleset = JSON.parse(json)
              const normalized = { ...newRules(parsed.timezone), ...parsed, rules: (parsed.rules ?? []).map(r => ({ ...r, id: r.id || newId(), windows: r.windows ?? [], conditions: r.conditions ?? [] })), triggers: (parsed.triggers ?? []).map(t => ({ ...t, id: t.id || newId(), readings: t.readings ?? 3, window_seconds: t.window_seconds ?? 1800 })) }
              await api(path + '/preview', { method: 'POST', body: JSON.stringify(normalized) })
              changeDraft(normalized); setNotice('Imported. Save to apply.')
            })}>Validate and import</button></div>
          </details>
        </div>
        <aside className="layout-aside" aria-label="Plan test">
          <section className="card automation-preview" aria-labelledby="preview-title">
            <div className="preview-head">
              <div><h2 id="preview-title">Test against now</h2><span className="hint">Uses live readings. Sends no commands.</span></div>
              <button type="button" className="button small" data-testid="automation-preview" disabled={busy}
                onClick={() => void perform(async () => { setPreview(await api<Preview>(path + '/preview', { method: 'POST', body: JSON.stringify(draft) })) })}>{preview ? 'Re-run' : 'Run test'}</button>
            </div>
            {!preview && <p className="hint preview-empty">Run the test to see which rule matches the current readings.</p>}
            {preview && <div data-testid="automation-preview-result">
              <div className="preview-result">
                <span>This plan would</span>
                <strong>{actionText(preview.decision.action)}</strong>
                <p>Matched: {preview.decision.explanation}</p>
                {!preview.target_available && <p>Device unavailable · setting retained</p>}
              </div>
              <ol className="preview-trace">
                {preview.decision.trace.map(r => <li key={r.rule_id} className={r.matches ? 'match' : ''}>
                  <Icon name={r.matches ? 'check' : 'dash'} />
                  <span><strong>{ruleLabel(r.rule_id)}</strong> <span>· {r.matches ? 'matches' : r.in_time ? 'conditions not met' : 'outside time window'}</span></span>
                </li>)}
              </ol>
              {Object.keys(preview.inputs).length > 0 && <div className="preview-inputs">
                <span className="reading-label">Inputs used</span>
                {Object.entries(preview.inputs).map(([key, input]) => {
                  const [kind, id, metric] = key.split(':')
                  const name = kind === 'outdoor' ? 'Outdoor station' : devices.find(d => d.id === id)?.name ?? 'Unavailable device'
                  return <div key={key}><span>{name} · {metricLabels[metric] ?? metric}</span><strong>{input.available ? String(input.value) + ' ' + input.unit : 'unavailable'}</strong></div>
                })}
              </div>}
            </div>}
          </section>
        </aside>
      </div>
    </>}
  </main>
}
