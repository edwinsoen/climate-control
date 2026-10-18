import { useState } from 'react'
import { ConnectionForm, type AccountConnection } from '../accounts/ConnectionForm'
import { Icon } from '../components/Icon'
import { automationMode, automationPath, configureMode, isPaused, type AutomationState } from '../devices/automation'
import { isOffline, type Api, type Device } from '../devices/types'
import { plural } from '../format'
import { OutdoorCard, type Outdoor } from '../outdoor/OutdoorCard'
import { ActivityFeed } from './ActivityFeed'
import { PurifierCard } from './PurifierCard'

function summary(devices: Device[]) {
  if (!devices.length) return 'No purifiers connected yet.'
  const supported = devices.filter((device) => device.supported)
  const automated = supported.filter((device) => automationMode(device.automation) !== 'off')
  const paused = automated.filter((device) => isPaused(device.automation)).length
  const auto = supported.filter((device) => automationMode(device.automation) === 'auto' && !isPaused(device.automation)).length
  const custom = automated.length - auto - paused
  const offline = devices.filter(isOffline).length
  if (!automated.length) return `${plural(devices.length, 'purifier')} connected. None automated yet.`
  const parts = [
    auto && `${plural(auto, 'purifier')} on Auto`, custom && `${custom} on custom rules`,
    paused && `${paused} overridden`, offline && `${offline} offline`,
  ].filter(Boolean)
  return parts.join(', ') + '.'
}

export function HomePage({ api, devices, outdoor, busy, connectError, onConnect, onDeviceChange, onOutdoorChange, onRefresh }: {
  api: Api; devices: Device[]; outdoor: Outdoor | null; busy: boolean; connectError: string
  onConnect: (connection: AccountConnection) => Promise<void>
  onDeviceChange: (device: Device) => void; onOutdoorChange: (outdoor: Outdoor) => void
  onRefresh: () => Promise<void>
}) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const supported = devices.filter((device) => device.supported)
  const automated = supported.filter((device) => automationMode(device.automation) !== 'off')
  const unpaused = automated.filter((device) => !isPaused(device.automation))
  const manualOnly = supported.length > 0 && automated.length === 0

  async function each(targets: Device[], action: (device: Device) => Promise<unknown>) {
    setWorking(true); setError('')
    try { await Promise.all(targets.map(action)) }
    catch (err) { setError((err as Error).message) }
    finally { await onRefresh().catch(() => {}); setWorking(false) }
  }

  const pauseAll = () => each(unpaused, async (device) => {
    const state = await api<AutomationState>(automationPath(device.id))
    await api(automationPath(device.id) + '/pause', { method: 'POST', body: JSON.stringify({ seconds: state.pause_seconds }) })
  })
  const resumeAll = () => each(automated, (device) => api(automationPath(device.id) + '/resume', { method: 'POST' }))

  return <main className="page" data-testid="devices-section">
    <div className="page-header">
      <div>
        <h1 data-testid="page-title">Home</h1>
        <p className="page-summary" data-testid={devices.length ? 'home-summary' : 'empty-devices'}>{summary(devices)}</p>
      </div>
      {automated.length > 0 && (unpaused.length > 0
        ? <button type="button" className="button" data-testid="pause-all" disabled={working} onClick={() => void pauseAll()}><Icon name="pause" />Pause all automation</button>
        : <button type="button" className="button" data-testid="resume-all" disabled={working} onClick={() => void resumeAll()}><Icon name="play" />Resume all automation</button>)}
    </div>
    {error && <p className="alert error" role="alert">{error}</p>}
    {manualOnly && <section className="setup-banner" data-testid="auto-banner" aria-labelledby="auto-banner-title">
      <span className="tile-icon"><Icon name="auto" /></span>
      <h2 id="auto-banner-title">Turn on Auto for {plural(supported.length, 'purifier')}</h2>
      <div className="actions">
        <button type="button" className="button primary large" data-testid="auto-enable-all" disabled={working}
          onClick={() => void each(supported, (device) => configureMode(api, device.id, 'auto'))}>Turn on Auto</button>
        <a className="text-button" href={'/devices/' + supported[0].id + '/rules'}>or write my own rules</a>
      </div>
    </section>}
    <div className="layout">
      {devices.length > 0 ? <section className="layout-main purifier-grid" aria-label="Purifiers">
        {devices.map((device) => <PurifierCard key={device.id} device={device} api={api} open={open.has(device.id)}
          onToggle={() => setOpen((current) => {
            const next = new Set(current)
            if (!next.delete(device.id)) next.add(device.id)
            return next
          })} onChange={onDeviceChange} onRefresh={onRefresh} />)}
      </section> : <section className="connect-panel" data-testid="home-connect" aria-labelledby="connect-title">
        <div className="connect-panel-head">
          <span className="tile-icon soft"><Icon name="air" /></span>
          <div><h2 id="connect-title">Connect a purifier account</h2><p>Use the account your purifiers are paired with.</p></div>
        </div>
        {connectError && <p className="alert error" role="alert" data-testid="form-error">{connectError}</p>}
        <ConnectionForm account={null} busy={busy} onSave={onConnect} autoFocus={false} />
      </section>}
      <aside className="layout-aside">
        {outdoor && <OutdoorCard key={outdoor.location_id} outdoor={outdoor} api={api} onChange={onOutdoorChange} />}
        <ActivityFeed api={api} devices={devices} />
      </aside>
    </div>
  </main>
}
