import { useEffect, useState } from 'react'
import { BandBadge, pm25Band } from '../bands'
import { Icon } from '../components/Icon'
import { DeviceControls } from '../devices/DeviceControls'
import { automationMode, automationPath, inhibitionLabels, isPaused, type AutomationState } from '../devices/automation'
import { ModeBadge } from '../devices/ModeBadge'
import { OverrideDuration } from '../devices/OverrideDuration'
import { deviceControls, isOffline, numeric, reading, settingText, type Api, type Device } from '../devices/types'
import { ago, clock } from '../format'
import { Sparkline } from './Sparkline'

export function PurifierCard({ device, api, open, onToggle, onChange, onRefresh }: {
  device: Device; api: Api; open: boolean; onToggle: () => void
  onChange: (device: Device) => void; onRefresh: () => Promise<void>
}) {
  const [automation, setAutomation] = useState<AutomationState | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const offline = isOffline(device)
  const mode = automationMode(device.automation)
  const paused = mode !== 'off' && isPaused(device.automation)
  const pm25 = numeric(device, 'pm25')
  const decision = device.automation?.summary?.decision
  const inhibition = device.automation?.summary?.inhibition
  const until = paused ? clock(device.automation!.paused_until) : ''

  useEffect(() => {
    if (!open || mode === 'off') return
    api<AutomationState>(automationPath(device.id)).then(setAutomation).catch(() => {})
  }, [api, device.id, open, mode])

  async function resume() {
    setBusy(true); setError('')
    try { await api(automationPath(device.id) + '/resume', { method: 'POST' }); await onRefresh() }
    catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  const overrideBar = paused && <div className="override-bar" data-testid="device-override">
    <span>{open ? <>Until <strong>{until}</strong></> : `Auto resumes at ${until}`}</span>
    <button type="button" className="button small" data-testid="device-resume" disabled={busy} onClick={() => void resume()}>
      {open ? 'Resume Auto' : 'Resume Auto now'}</button>
  </div>

  return <article className={`purifier-card${open ? ' is-open' : ''}${offline ? ' is-offline' : ''}`} data-testid="device-card">
    <div className="purifier-head">
      <div>
        <a className="purifier-name" href={'/devices/' + device.id} data-testid="device-name">{device.name}</a>
        <span className="purifier-model">{device.model}</span>
      </div>
      <div className="purifier-head-actions">
        {device.supported && <ModeBadge mode={mode} paused={paused} />}
        {device.supported && <button type="button" className="icon-button controls-toggle" data-testid="device-controls-toggle"
          aria-label={open ? undefined : `Controls for ${device.name}${offline ? ' (offline)' : ''}`} aria-expanded={open}
          disabled={offline && !open} onClick={onToggle}>{open ? 'Done' : <Icon name="sliders" />}</button>}
      </div>
    </div>
    {!device.supported && <p className="subtle">This purifier model is not supported yet.</p>}
    {device.supported && open && <div className="quick-controls">
      {overrideBar}
      <DeviceControls device={device} {...deviceControls(api, device, onChange)} />
      <div className="quick-controls-footer">
        {mode !== 'off' && automation ? <OverrideDuration api={api} deviceId={device.id} state={automation} onChange={setAutomation} />
          : <span className="hint">{mode === 'off' ? 'No automation' : ''}</span>}
        <a href={'/devices/' + device.id}>Open device</a>
      </div>
    </div>}
    {device.supported && !open && <>
      <div className="purifier-reading">
        <div>
          <span className="reading-label">{offline || device.stale ? `PM2.5 · ${ago(device.retrieved_at)}` : 'PM2.5'}</span>
          <div className="reading-value">
            <strong data-testid="device-pm25" className={pm25 === null ? 'text-value' : undefined}>{reading(device, 'pm25')}</strong>
            {pm25 !== null && <span className="reading-unit">µg/m³</span>}
          </div>
          {offline ? <BandBadge band="offline" testId="device-status" /> : pm25 !== null && <BandBadge band={pm25Band(pm25)} />}
        </div>
        <div className="purifier-setting">
          <span className="reading-label">{offline ? 'Last setting' : 'Setting'}</span>
          <strong data-testid="device-setting">{settingText(device.state)}</strong>
          <span className="reading-time">{paused ? 'set by you' : offline ? '' : ago(device.retrieved_at)}</span>
        </div>
      </div>
      {device.capabilities.includes('pm25') && (offline
        ? <div className="sparkline-empty">No readings since {device.retrieved_at ? clock(Date.parse(device.retrieved_at) / 1000) : 'connecting'}</div>
        : <Sparkline api={api} deviceId={device.id} name={device.name} />)}
      {overrideBar}
      {!paused && mode !== 'off' && (decision || inhibition) && <p className="rule-line" data-testid="device-rule">
        {decision && <><span className="label">Rule</span> <strong>{decision.explanation}</strong></>}
        {inhibition && !['already_applied', 'hold'].includes(inhibition) && <span className="label">{decision ? ' · ' : ''}{inhibitionLabels[inhibition] ?? inhibition}</span>}
      </p>}
    </>}
    {(error || device.error) && <p className="inline-error" role="alert">{error || device.error}</p>}
    {device.supported && !open && <div className="stat-line">
      {device.capabilities.includes('temperature') && <span data-testid="device-temperature">{reading(device, 'temperature', ' °C')}</span>}
      {device.capabilities.includes('humidity') && <span><span data-testid="device-humidity">{reading(device, 'humidity', '%')}</span> humidity</span>}
      {device.capabilities.includes('filter_life') && <span>Filter <span data-testid="device-filter-life">{reading(device, 'filter_life', '%')}</span></span>}
      {!device.capabilities.includes('temperature') && <span>No temperature sensor</span>}
    </div>}
  </article>
}
