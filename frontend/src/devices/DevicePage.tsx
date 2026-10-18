import { useEffect, useRef, useState } from 'react'
import { BandBadge, pm25Band } from '../bands'
import { Icon, type IconName } from '../components/Icon'
import { ago, clock, plural } from '../format'
import { AirQualityChart, type ChartMetric } from './AirQualityChart'
import {
  automationMode, automationPath, configureMode, conditionText, eventDetail, eventText, inhibitionLabels, isActivity, isPaused,
  modeDescriptions, modeLabels, runText, shortActionText, stepText, windowText,
  type AutomationEvent, type AutomationState, type Mode, type Run, type Step,
} from './automation'
import { DeviceControls } from './DeviceControls'
import { OverrideDuration } from './OverrideDuration'
import { deviceControls, isOffline, numeric, reading, settingText, type Api, type Device } from './types'

const CHART_METRICS: { id: ChartMetric; label: string; unit: string }[] = [
  { id: 'pm25', label: 'PM2.5', unit: 'µg/m³' },
  { id: 'pm10', label: 'PM10', unit: 'µg/m³' },
  { id: 'pm1', label: 'PM1', unit: 'µg/m³' },
  { id: 'temperature', label: 'Temperature', unit: '°C' },
  { id: 'humidity', label: 'Humidity', unit: '%' },
]
const FACTS: [string, string, string][] = [['pm1', 'PM1', ' µg/m³'], ['pm10', 'PM10', ' µg/m³'], ['temperature', 'Temperature', ' °C'], ['humidity', 'Humidity', '%']]
const MODE_ICONS: Record<Mode, IconName> = { auto: 'auto', custom: 'list', off: 'hand' }

function Tokens({ parts }: { parts: string[] }) {
  return <>{parts[0]} <span className="token">{parts[1]}</span></>
}

export function DevicePage({ device, devices, api, onChange }: { device: Device; devices: Device[]; api: Api; onChange: (device: Device) => void }) {
  const path = automationPath(device.id)
  const [state, setState] = useState<AutomationState | null>(null)
  const [events, setEvents] = useState<AutomationEvent[]>([])
  const [steps, setSteps] = useState<Step[]>([])
  const [runOpen, setRunOpen] = useState(false)
  const [metric, setMetric] = useState<ChartMetric>('pm25')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const stateRef = useRef<AutomationState | null>(null)
  stateRef.current = state
  const offline = isOffline(device)
  const mode = automationMode(state ?? device.automation)
  const paused = mode !== 'off' && isPaused(state ?? device.automation)
  const run: Run | null = state?.run ?? null
  const running = run?.status === 'running'
  const pendingReview = state?.owner === 'agent' && ['pending', 'claimed'].includes(state.job?.status ?? '')
  const pm25 = numeric(device, 'pm25')
  const decision = state?.summary?.decision
  const inhibition = state?.summary?.inhibition
  const metrics = CHART_METRICS.filter((item) => device.capabilities.includes(item.id))
  const chartMetric = metrics.find((item) => item.id === metric) ?? metrics[0] ?? CHART_METRICS[0]

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const refresh = async () => {
      if (!document.hidden) {
        try {
          const [value, list] = await Promise.all([api<AutomationState>(path), api<AutomationEvent[]>(path + '/events')])
          if (!cancelled) { setState(value); setEvents(list) }
        } catch (err) { if (!cancelled && !stateRef.current) setError((err as Error).message) }
      }
      const current = stateRef.current
      const active = current?.run?.status === 'running' || ['pending', 'claimed'].includes(current?.job?.status ?? '')
      if (!cancelled) timer = window.setTimeout(() => void refresh(), active ? 3000 : 15000)
    }
    void refresh()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [api, path])

  useEffect(() => { if (running) setRunOpen(true) }, [running, run?.id])

  useEffect(() => {
    if (!runOpen && !running) return
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
  }, [path, api, runOpen, running, run?.id, run?.step_count])

  async function perform(task: () => Promise<AutomationState | void>) {
    setBusy(true); setError('')
    try { const next = await task(); if (next) setState(next) }
    catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  const ruleset = state?.ruleset
  const deviceName = (id: string | null | undefined) => !id || id === device.id ? null : devices.find((item) => item.id === id)?.name ?? 'Unavailable purifier'
  const activity = events.filter(isActivity).slice(0, 8)
  const runSteps = steps.map((step) => [step, stepText(step)] as const).filter(([, text]) => text)

  return <main className="page detail" data-testid="device-page">
    <div className="page-header">
      <div>
        <nav className="breadcrumb" aria-label="Breadcrumb"><a href="/" data-testid="device-back">Home</a><span aria-hidden="true">/</span><span aria-current="page">{device.name}</span></nav>
        <h1 data-testid="page-title">{device.name}</h1>
        <div className="meta-row">
          <span className={`status-dot ${offline ? 'offline' : device.stale ? '' : 'online'}`} data-testid="device-status">{offline ? 'Offline' : device.stale ? 'Stale' : 'Online'}</span>
          <span>Read {ago(device.retrieved_at).toLowerCase()}</span><span>{device.model}</span><span>{device.account_label}</span>
        </div>
      </div>
      {device.supported && <div className="mode-control">
        <div className="segmented" role="radiogroup" aria-label="Automation mode">
          {(['auto', 'custom', 'off'] as Mode[]).map((option) => <button key={option} type="button" role="radio" data-testid={`automation-mode-${option}`}
            aria-checked={mode === option} disabled={!state || busy || (option === 'custom' && !state.revision)}
            title={option === 'custom' && state && !state.revision ? 'Save a plan first' : undefined}
            onClick={() => { if (state && mode !== option) void perform(() => configureMode(api, device.id, option, state)) }}>
            <Icon name={MODE_ICONS[option]} />{modeLabels[option]}</button>)}
        </div>
        <p data-testid="automation-status">{modeDescriptions[mode]}{paused && state && <> · <span data-testid="automation-pause-status">Paused until {clock(state.paused_until)}</span></>}</p>
      </div>}
    </div>
    {error && <p className="alert error" role="alert" data-testid="automation-error">{error}</p>}
    {device.error && <p className="alert error" role="status">{device.error}</p>}
    {!device.supported && <p className="alert neutral">This purifier model is not supported yet.</p>}
    {device.supported && <div className="layout device-layout">
      <div className="layout-main">
        <section className="card flush now-panel order-1" aria-label="Now">
          <div className="now-reading">
            <div className="form-stack">
              <span className="reading-label">{pm25 !== null ? 'PM2.5 now' : 'Air quality now'}</span>
              <div className="reading-value">
                <strong data-testid="device-pm25" className={pm25 === null ? 'text-value' : undefined}>{pm25 !== null ? reading(device, 'pm25') : reading(device, 'air_quality')}</strong>
                {pm25 !== null && <span className="reading-unit">µg/m³</span>}
              </div>
              {offline ? <BandBadge band="offline" /> : pm25 !== null && <BandBadge band={pm25Band(pm25)} />}
            </div>
            {FACTS.some(([key]) => device.capabilities.includes(key)) && <dl className="now-facts">
              {FACTS.filter(([key]) => device.capabilities.includes(key)).map(([key, label, unit]) => <div key={key}>
                <dt>{label}</dt><dd data-testid={`device-${key}`}>{reading(device, key, unit)}</dd></div>)}
            </dl>}
          </div>
          <div className={`now-auto${mode === 'off' || paused ? ' inactive' : ''}`}>
            <div className="now-auto-head">
              <span>{mode === 'off' ? 'Automation off' : paused ? 'Overridden' : mode === 'auto' ? 'Auto is running' : 'Custom rules running'}</span>
              <span className="mono">{ago(device.retrieved_at)}</span>
            </div>
            <div className="now-setting" data-testid="device-setting">{settingText(device.state)}</div>
            {mode === 'off'
              ? <p className="subtle">No automation. Changes come from the controls or the purifier.</p>
              : <dl className="decision">
                <div><dt>Matched</dt><dd data-testid="device-rule">{decision?.explanation ?? 'Waiting for a decision'}{decision && <span>{shortActionText(decision.action)}</span>}</dd></div>
                {inhibition && <div><dt>Status</dt><dd>{inhibitionLabels[inhibition] ?? inhibition}</dd></div>}
                {pm25 !== null && <div><dt>Inputs</dt><dd>PM2.5 {pm25} µg/m³ <span>read {ago(device.retrieved_at).toLowerCase()}</span></dd></div>}
              </dl>}
          </div>
        </section>

        <section className="card order-3">
          <AirQualityChart key={chartMetric.id} deviceId={device.id} name={device.name} capabilities={device.capabilities} fetchHistory={api}
            metric={chartMetric.id} unit={chartMetric.unit} collapsible={false}
            title={metrics.length ? `${chartMetric.label} and fan speed` : 'Fan speed'} subtitle={metrics.length ? `${chartMetric.unit} · settings below` : undefined}
            metricSelector={metrics.length > 1 && <label className="inline-field">Metric
              <select aria-label={`History metric for ${device.name}`} data-testid="device-history-metric" value={chartMetric.id}
                onChange={(event) => setMetric(event.target.value as ChartMetric)}>
                {metrics.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select></label>} />
        </section>

        <section className="card flush order-4" aria-labelledby="plan-title" data-testid="device-plan">
          <div className="card-header card-section-header">
            <div><h2 id="plan-title">Plan</h2>
              <span className="hint">{ruleset
                ? `Written by ${state?.owner === 'agent' ? 'Auto' : 'you'} · revision ${state?.revision}${mode === 'off' ? ' · kept, not running' : ''}. First matching rule wins.`
                : 'No plan yet.'}</span></div>
            <div className="actions">
              {state?.owner === 'agent' && mode === 'auto' && <button type="button" className="button small" data-testid="automation-reconsider" disabled={busy || pendingReview}
                onClick={() => void perform(() => api<AutomationState>(path + '/reconsider', { method: 'POST' }))}>Ask Auto to review</button>}
              <a className="button small" data-testid="device-rules-link" href={'/devices/' + device.id + '/rules'}>{state?.owner === 'agent' && ruleset ? 'Edit as custom' : 'Edit plan'}</a>
            </div>
          </div>
          {pendingReview && <p className="plan-empty" data-testid="automation-review-status">{running ? 'Auto is reviewing the plan…' : 'Auto review pending'}</p>}
          {ruleset && <ol className="plan-list">
            {ruleset.rules.map((rule, index) => <li key={rule.id}>
              <span className="plan-index">{index + 1}</span>
              <div>
                <div className="plan-condition">
                  {!rule.windows.length && !rule.conditions.length && 'Always'}
                  {rule.windows.map((window, wi) => <span key={'w' + wi}>{wi > 0 && 'or '}<Tokens parts={windowText(window)} /></span>)}
                  {rule.conditions.map((condition, ci) => <span key={'c' + ci}>{(ci > 0 || rule.windows.length > 0) && 'and '}<Tokens parts={conditionText(condition, deviceName)} /></span>)}
                  {decision?.rule_id === rule.id && <span className="active-pill">Active now</span>}
                </div>
                <span className="plan-name">{rule.explanation}</span>
              </div>
              <span className="plan-action">{shortActionText(rule.action)}</span>
            </li>)}
            <li className="fallback">
              <span className="plan-index">·</span>
              <div className="plan-condition">Fallback {decision && decision.rule_id === null && <span className="active-pill">Active now</span>}</div>
              <span className="plan-action">{shortActionText(ruleset.fallback)}</span>
            </li>
          </ol>}
          {!ruleset && !pendingReview && <p className="plan-empty">{mode === 'auto' ? 'Auto writes the first plan after it reviews this purifier.' : 'Write a plan to automate this purifier.'}</p>}
          {ruleset && <div className="plan-footer">
            {ruleset.triggers.length > 0 && <span>Review triggers: {plural(ruleset.triggers.length, 'condition')}</span>}
            <span>Changes need {plural(ruleset.consecutive_readings, 'reading')} · at least {Math.round(ruleset.min_change_seconds / 60)} min apart</span>
          </div>}
        </section>
      </div>

      <aside className="layout-aside">
        <section className="card order-2" aria-labelledby="controls-title">
          <div className="card-header">
            <div><h2 id="controls-title">Controls</h2>
              {state && mode !== 'off' && <OverrideDuration api={api} deviceId={device.id} state={state} onChange={setState} label="Changes override Auto for" />}</div>
            {state && mode !== 'off' && (paused
              ? <button type="button" className="button small" data-testid="automation-resume" disabled={busy}
                onClick={() => void perform(() => api<AutomationState>(path + '/resume', { method: 'POST' }))}>Resume Auto</button>
              : <button type="button" className="button small" data-testid="automation-pause" disabled={busy}
                onClick={() => void perform(() => api<AutomationState>(path + '/pause', { method: 'POST', body: JSON.stringify({ seconds: state.pause_seconds }) }))}><Icon name="pause" />Pause</button>)}
          </div>
          <DeviceControls device={device} {...deviceControls(api, device, onChange)} />
        </section>

        <section className="card activity-card order-5" aria-labelledby="device-activity-title" data-testid="device-activity">
          <h2 id="device-activity-title">Activity</h2>
          {!activity.length && !run && <p className="subtle">No activity yet.</p>}
          <ol className="activity-list" data-testid="automation-events">
            {run && <li>
              <time>{clock(run.started_at)}</time>
              <div>
                <strong data-testid="agent-run-status">{runText(run)}</strong>
                <span>{run.model} · {plural(run.llm_calls, 'model call')}{run.finished_at ? ' · ' + Math.max(1, Math.round(run.finished_at - run.started_at)) + 's' : ''}</span>
                <details className="disclosure" data-testid="agent-activity" open={runOpen} onToggle={(event) => setRunOpen(event.currentTarget.open)}>
                  <summary>{running ? 'What Auto is doing' : 'How Auto worked'}</summary>
                  <ol className="agent-steps" data-testid="agent-steps">
                    {runSteps.map(([step, text], index) => <li key={index} className={'agent-step ' + step.kind}>{text}</li>)}
                  </ol>
                  {running && !runSteps.length && <p className="hint">Reading this device's data…</p>}
                </details>
              </div>
            </li>}
            {activity.map((event) => <li key={event.id}>
              <time dateTime={new Date(event.created_at * 1000).toISOString()}>{clock(event.created_at)}</time>
              <div><strong>{eventText(event)}</strong>{eventDetail(event) && <span>{eventDetail(event)}</span>}</div>
            </li>)}
          </ol>
        </section>

        <section className="card order-6" aria-labelledby="details-title">
          <h2 id="details-title">Purifier</h2>
          <dl className="details-list">
            {device.capabilities.includes('filter_life') && <div><dt>Filter remaining</dt><dd data-testid="device-filter-life">{reading(device, 'filter_life', '%')}</dd></div>}
            {device.capabilities.includes('air_quality') && <div><dt>Air quality</dt><dd className="state">{reading(device, 'air_quality')}</dd></div>}
            {device.capabilities.includes('display') && <div><dt>Display</dt><dd className="state" data-testid="device-display-status">{reading(device, 'display')}</dd></div>}
            <div><dt>Account</dt><dd>{device.account_label}</dd></div>
            {state && <div><dt>Time zone</dt><dd>{state.timezone}</dd></div>}
          </dl>
        </section>
      </aside>
    </div>}
  </main>
}
