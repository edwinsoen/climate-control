import { useEffect, useState } from 'react'
import { AccountCard } from '../accounts/AccountCard'
import type { Account } from '../accounts/types'
import { AgentAccess, type AgentStatus } from '../agent/AgentAccess'
import { Icon } from '../components/Icon'
import { automationMode } from '../devices/automation'
import { WARNING_THRESHOLD_OPTIONS, type WarningThresholdId } from '../devices/DeviceControls'
import type { Api, Device } from '../devices/types'
import { LocationForm } from '../outdoor/LocationForm'
import { age, type Outdoor } from '../outdoor/OutdoorCard'

const SECTIONS = [['accounts', 'Accounts'], ['outdoor', 'Outdoor air'], ['auto', 'Auto planner'], ['warnings', 'Display warnings'], ['agents', 'Agent access']]
const providerLabels: Record<string, string> = { openrouter: 'OpenRouter', gemini: 'Gemini', remote: 'Agent Runtime' }

export function SettingsPage({ api, accounts, devices, outdoor, notice, onConnect, onReconnect, onRemove, onOutdoorChange }: {
  api: Api; accounts: Account[]; devices: Device[]; outdoor: Outdoor | null; notice: string
  onConnect: () => void; onReconnect: (account: Account) => void; onRemove: (account: Account) => Promise<boolean>
  onOutdoorChange: (outdoor: Outdoor) => void
}) {
  const [section, setSection] = useState(() => window.location.hash.slice(1) || 'accounts')
  const [status, setStatus] = useState<AgentStatus>()

  useEffect(() => {
    void api<AgentStatus>('/api/agent/status').then(setStatus).catch(() => {})
  }, [api])

  useEffect(() => {
    const update = () => setSection(window.location.hash.slice(1) || 'accounts')
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])

  const onAuto = devices.filter((device) => automationMode(device.automation) === 'auto').map((device) => device.name)
  return <main className="page detail" data-testid="settings-page">
    <div className="page-header"><div>
      <nav className="breadcrumb" aria-label="Breadcrumb"><a href="/">Home</a><span aria-hidden="true">/</span><span aria-current="page">Settings</span></nav>
      <h1 data-testid="page-title">Settings</h1>
    </div></div>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="Settings sections">
        {SECTIONS.map(([id, label]) => <a key={id} href={'#' + id} aria-current={section === id ? 'true' : undefined}>{label}</a>)}
      </nav>
      <div className="settings-sections">
        <section id="accounts" className="settings-section flush" aria-labelledby="accounts-title">
          <div className="card-header">
            <div><h2 id="accounts-title">Accounts</h2></div>
            <button type="button" className="button small primary" data-testid="connect-account" onClick={onConnect}><Icon name="plus" />Connect account</button>
          </div>
          {notice && <p className="alert notice" role="status">{notice}</p>}
          {accounts.length > 0 ? <div className="table-wrap"><table className="table" aria-labelledby="accounts-title">
            <thead><tr><th scope="col">Account</th><th scope="col">Region</th><th scope="col">Purifiers</th><th scope="col">Status</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
            {accounts.map((account) => <AccountCard key={account.id} account={account} onReconnect={onReconnect} onRemove={onRemove}
              purifiers={devices.filter((device) => device.account_id === account.id).map((device) => device.name)} />)}
          </table></div> : <p className="table-empty" data-testid="empty-title">No accounts connected.</p>}
        </section>

        <OutdoorSection api={api} outdoor={outdoor} onChange={onOutdoorChange} />

        <section id="auto" className="settings-section" aria-labelledby="auto-title">
          <h2 id="auto-title">Auto planner</h2>
          {status && <dl className="status-list">
            <div><dt>Status</dt><dd data-testid="agent-status"><span className={`status-dot ${status.enabled && status.provider ? 'online' : ''}`}>
              {status.enabled && status.provider ? 'Ready' : 'Not configured'}</span></dd></div>
            <div><dt>Model</dt><dd>{status.enabled && status.provider
              ? [providerLabels[status.provider] ?? status.provider, status.model].filter(Boolean).join(' · ') : 'None'}</dd></div>
            <div><dt>Purifiers on Auto</dt><dd>{onAuto.length ? onAuto.join(', ') : 'None'}</dd></div>
          </dl>}
          <p className="subtle">{status?.enabled && status.provider
            ? <>Model set by <span className="code">AGENT_MODEL</span>.</>
            : 'Auto-managed purifiers wait for an external planner connected through Agent access.'}</p>
        </section>

        <DisplaySettings api={api} />
        <AgentAccess api={api} status={status} />
      </div>
    </div>
  </main>
}

function OutdoorSection({ api, outdoor, onChange }: { api: Api; outdoor: Outdoor | null; onChange: (outdoor: Outdoor) => void }) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function remove() {
    setBusy(true); setError('')
    try { onChange(await api<Outdoor>('/api/outdoor/location', { method: 'DELETE' })) }
    catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  const updated = outdoor?.metrics.aqi?.measured_at
  return <section id="outdoor" className="settings-section" aria-labelledby="settings-outdoor-title" data-testid="settings-outdoor">
    <h2 id="settings-outdoor-title">Outdoor air</h2>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {outdoor && !outdoor.available && <p className="subtle">Outdoor data is not connected.</p>}
    {outdoor?.available && editing && <LocationForm api={api} onCancel={() => setEditing(false)} onSave={(next) => { setEditing(false); onChange(next) }} />}
    {outdoor?.available && !editing && <div className="station-row">
      <div>
        <span className="tile-icon soft"><Icon name="pin" /></span>
        <div>
          <strong data-testid="settings-outdoor-station">{outdoor.configured ? outdoor.station?.name ?? 'Connecting station…' : 'No station selected'}</strong>
          <span className="hint">AQICN / WAQI{updated ? ` · updated ${age(updated).toLowerCase()}` : ''}</span>
        </div>
      </div>
      <div className="actions">
        <button type="button" className="button small" data-testid="settings-outdoor-change" disabled={busy} onClick={() => { setEditing(true); setError('') }}>
          {outdoor.configured ? 'Change station' : 'Set location'}</button>
        {outdoor.configured && <button type="button" className="button small ghost danger" data-testid="settings-outdoor-remove" disabled={busy} onClick={() => void remove()}>Remove</button>}
      </div>
    </div>}
  </section>
}

function DisplaySettings({ api }: { api: Api }) {
  const [threshold, setThreshold] = useState<WarningThresholdId>('who_2021_24h')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void api<{ warning_threshold: WarningThresholdId }>('/api/display-settings')
      .then((data) => setThreshold(data.warning_threshold))
      .catch(() => {})
  }, [api])

  async function update(next: WarningThresholdId) {
    const previous = threshold
    setThreshold(next); setBusy(true); setError('')
    try {
      const saved = await api<{ warning_threshold: WarningThresholdId }>('/api/display-settings', {
        method: 'PUT', body: JSON.stringify({ warning_threshold: next }),
      })
      setThreshold(saved.warning_threshold)
    } catch (err) { setThreshold(previous); setError((err as Error).message) } finally { setBusy(false) }
  }

  return <section id="warnings" className="settings-section" data-testid="display-settings" aria-labelledby="display-settings-title">
    <div><h2 id="display-settings-title">Display warnings</h2><p className="subtle">Default for displays in Warnings only mode without a device override.</p></div>
    {error && <p className="alert error" role="alert">{error}</p>}
    <div className="table-wrap boxed"><table className="table boxed">
      <caption className="visually-hidden">Warning threshold. A warning shows when a reading is above the value.</caption>
      <thead><tr><th scope="col" className="select-cell"><span className="visually-hidden">Selected</span></th><th scope="col">Guideline</th>
        <th scope="col" className="numeric">PM2.5 above</th><th scope="col" className="numeric">PM10 above</th></tr></thead>
      <tbody>{WARNING_THRESHOLD_OPTIONS.map((option) => <tr key={option.id} className={threshold === option.id ? 'selected' : undefined}>
        <td><input type="radio" name="warning-threshold" id={`warning-threshold-${option.id}`} data-testid={`warning-threshold-${option.id}`}
          checked={threshold === option.id} disabled={busy} onChange={() => void update(option.id)} /></td>
        <td><label htmlFor={`warning-threshold-${option.id}`}>{option.name}</label></td>
        <td className="numeric">{option.pm25} <span className="hint">µg/m³</span></td>
        <td className="numeric">{option.pm10} <span className="hint">µg/m³</span></td>
      </tr>)}</tbody>
    </table></div>
  </section>
}
