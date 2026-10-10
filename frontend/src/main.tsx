import { StrictMode, useCallback, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ConnectionForm, type AccountConnection } from './accounts/ConnectionForm'
import { AccountCard } from './accounts/AccountCard'
import type { Account } from './accounts/types'
import { AirQualityChart } from './devices/AirQualityChart'
import { DeviceControls, type ControlOptions } from './devices/DeviceControls'
import { UserMenu } from './UserMenu'
import './styles.css'

type Session = {
  authenticated: boolean; identity: string | null; csrf_token: string | null
  google_enabled: boolean; local_enabled: boolean
}
type Device = {
  id: string; account_id: string; account_label: string; name: string; model: string
  supported: boolean; capabilities: string[]; state: Record<string, string | number | null>
  retrieved_at: string | null; stale: boolean; error: string | null
  controls: ControlOptions; control_available: boolean; control_retry_at: string | null
}

function newerDevice(current: Device | undefined, incoming: Device): Device {
  return current && (Date.parse(current.retrieved_at ?? '') || 0) > (Date.parse(incoming.retrieved_at ?? '') || 0) ? current : incoming
}

function Icon({ kind = 'air' }: { kind?: 'air' | 'arrow' | 'plus' }) {
  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
    {kind === 'air' && <><path d="M3 8h12a3 3 0 1 0-3-3M3 12h16a3 3 0 1 1-3 3M3 16h6a3 3 0 1 1-3 3" /></>}
    {kind === 'arrow' && <><path d="m14 5 7 7-7 7M3 12h18" /></>}
    {kind === 'plus' && <path d="M12 5v14M5 12h14" />}
  </svg>
}

function ago(value: string | null): string {
  if (!value) return 'No reading yet'
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000))
  if (seconds < 60) return 'Just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`
  return `${Math.floor(seconds / 86400)} days ago`
}

function DeviceCard({ device, api, onChange }: { device: Device; api: <T>(path: string, options?: RequestInit) => Promise<T>; onChange: (device: Device) => void }) {
  const offline = device.state.connectivity === 'offline'
  const status = !device.supported ? 'Unsupported' : offline ? 'Offline' : device.stale ? 'Stale' : 'Connected'
  const reading = (key: string, suffix = '') => {
    if (!device.capabilities.includes(key)) return 'Not supported'
    const value = device.state[key]
    return value === null || value === undefined || value === 'unknown' ? 'Unknown' : `${value}${suffix}`
  }
  return <article className="device-card" data-testid="device-card">
    <div className="card-heading">
      <div className="device-icon"><Icon /></div>
      <span className={`badge ${status === 'Connected' ? 'good' : 'muted'}`}><i />{status}</span>
    </div>
    <p className="eyebrow">{device.account_label}</p>
    <h3 data-testid="device-name">{device.name}</h3><p className="model">{device.model}</p>
    {device.supported && <>
      <div className="main-reading"><div><span>PM2.5</span><strong data-testid="device-pm25">{reading('pm25')}</strong></div>
        <div className="reading-meta">
          {device.capabilities.includes('pm25') && device.state.pm25 != null && <><span className="unit">µg/m³</span><span aria-hidden="true">·</span></>}
          <small className="reading-time">{ago(device.retrieved_at)}{device.stale && device.retrieved_at && ' · Previous readings'}</small>
        </div>
      </div>
      <DeviceControls device={device} onControl={async (command) => {
        const updated = await api<Device & { control_confirmed: boolean }>(`/api/devices/${device.id}/control`, { method: 'POST', body: JSON.stringify(command) })
        onChange(updated)
        return { retryAt: updated.control_retry_at, confirmed: updated.control_confirmed }
      }} />
      <dl className="device-details">
        <div><dt>Air quality</dt><dd>{reading('air_quality')}</dd></div>
        <div><dt>Filter remaining</dt><dd data-testid="device-filter-life">{reading('filter_life', '%')}</dd></div>
        {device.capabilities.includes('pm1') && <div><dt>PM1</dt><dd>{reading('pm1', ' µg/m³')}</dd></div>}
        {device.capabilities.includes('pm10') && <div><dt>PM10</dt><dd>{reading('pm10', ' µg/m³')}</dd></div>}
      </dl>
    </>}
    {device.error && <p className="inline-error">{device.error}</p>}
    <AirQualityChart deviceId={device.id} name={device.name} capabilities={device.capabilities} fetchHistory={api} />
  </article>
}

function App() {
  const [session, setSession] = useState<Session | null>(null)
  const sessionRef = useRef<Session | null>(null)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [devices, setDevices] = useState<Device[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<Account | null | undefined>(undefined)
  const accountsPage = window.location.pathname === '/accounts'

  const updateSession = useCallback((value: Session) => {
    sessionRef.current = value
    setSession(value)
  }, [])

  const api = useCallback(async <T,>(path: string, options: RequestInit = {}): Promise<T> => {
    const response = await fetch(path, {
      ...options, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json',
        'X-CSRF-Token': sessionRef.current?.csrf_token ?? '', ...options.headers },
    })
    const data = await response.json()
    if (!response.ok) {
      if (response.status === 401 && sessionRef.current) {
        updateSession({ ...sessionRef.current, authenticated: false, csrf_token: null })
      }
      const detail = Array.isArray(data.detail) ? data.detail.map((item: { msg: string }) => item.msg).join('. ') : data.detail
      throw new Error(detail ?? 'The request could not be completed.')
    }
    return data as T
  }, [updateSession])

  const load = useCallback(async () => {
    const [nextAccounts, nextDevices] = await Promise.all([
      api<Account[]>('/api/accounts'), api<Device[]>('/api/devices'),
    ])
    setAccounts(nextAccounts)
    setDevices((current) => nextDevices.map((incoming) => newerDevice(current.find((item) => item.id === incoming.id), incoming)))
  }, [api])

  useEffect(() => {
    api<Session>('/api/session').then(updateSession).catch((err: Error) => setError(err.message)).finally(() => setLoading(false))
  }, [api, updateSession])

  useEffect(() => {
    if (!session?.authenticated) return
    const reload = () => load().catch((err: Error) => setError(err.message))
    void reload()
    const timer = window.setInterval(() => { if (!document.hidden) void reload() }, 15000)
    const visible = () => { if (!document.hidden) void reload() }
    document.addEventListener('visibilitychange', visible)
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', visible) }
  }, [session?.authenticated, load])

  useEffect(() => {
    if (editing === undefined) return
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) { setEditing(undefined); setError('') }
      if (event.key !== 'Tab' || !dialog) return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]'))
      const first = focusable[0], last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [editing, busy])

  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('')
    const form = new FormData(event.currentTarget)
    try {
      await api('/api/login', { method: 'POST', body: JSON.stringify(Object.fromEntries(form)) })
      updateSession(await api<Session>('/api/session'))
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  async function saveAccount(connection: AccountConnection) {
    setBusy(true); setError(''); setNotice('')
    try {
      const account = await api<Account>(editing ? `/api/accounts/${editing.id}` : '/api/accounts', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(connection),
      })
      setEditing(undefined); await load()
      setNotice(account.error ?? (account.status === 'pending' ? 'Connecting account…' : 'Account connected.'))
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  async function remove(account: Account) {
    setError('')
    try { await api(`/api/accounts/${account.id}`, { method: 'DELETE' }); await load(); return true }
    catch (err) { setError((err as Error).message); return false }
  }

  const brand = <a className="brand" href="/"><span className="brand-icon"><Icon /></span>climate<span className="brand-light">control</span></a>
  if (loading) return <main className="loading" aria-live="polite">Loading Climate Control…</main>
  if (!session?.authenticated) return <main className="login-layout">
    <section className="login-story">{brand}<div>
      <h1>Manage your<br />climate devices.</h1>
      <div className="story-lines" aria-hidden="true"><span /><span /><span /></div>
    </div></section>
    <section className="login-panel"><div className="login-box">
      <h2 data-testid="login-title">Sign in</h2>
      {error && <p className="alert error" data-testid="form-error" role="alert">{error}</p>}
      {new URLSearchParams(window.location.search).get('error') === 'google' && <p className="alert error" role="alert">Google sign-in could not be completed. Use an allowed account or try again.</p>}
      {session?.google_enabled && <a className="button google" href="/auth/google/login">Continue with Google <Icon kind="arrow" /></a>}
      {session?.google_enabled && session.local_enabled && <p className="divider">or sign in as administrator</p>}
      {session?.local_enabled && <form onSubmit={login}>
        <label>Username<input name="username" autoComplete="username" required defaultValue="admin" /></label>
        <label>Password<input data-testid="login-password" name="password" type="password" autoComplete="current-password" required /></label>
        <button className="button primary full" data-testid="login-submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}<Icon kind="arrow" /></button>
      </form>}
      {!session && <button className="button" onClick={() => window.location.reload()}>Try again</button>}
    </div></section>
  </main>

  return <div className="app-shell">
    <header className="app-header" data-testid="app-header">{brand}<nav aria-label="Main navigation">
      <a data-testid="nav-overview" href="/" className={!accountsPage ? 'active' : ''}>Overview</a>
      <a data-testid="nav-accounts" href="/accounts" className={accountsPage ? 'active' : ''}>Accounts</a>
    </nav>
    <button className="button primary small header-connect" data-testid="connect-account" onClick={() => { setError(''); setEditing(null) }}><Icon kind="plus" />Connect account</button>
    <UserMenu identity={session.identity} onSignOut={async () => {
      try { await api('/api/logout', { method: 'POST' }); updateSession(await api<Session>('/api/session')) }
      catch (err) { setError((err as Error).message) }
    }} /></header>
    <main className="content">
      {accountsPage && <div className="page-heading"><h1 data-testid="page-title">Connected accounts</h1></div>}
      {error && editing === undefined && <p className="alert error" data-testid="form-error" role="alert">{error}</p>}
      {accountsPage && notice && <p className="alert notice" role="status">{notice}</p>}
      {accountsPage && accounts.length > 0 && <section className="account-list" aria-label="Connected accounts">
        {accounts.map((account) => <AccountCard key={account.id} account={account}
          onReconnect={(value) => { setError(''); setEditing(value) }} onRemove={remove} />)}
      </section>}
      {!accountsPage && <section data-testid="devices-section" aria-labelledby="devices-title">
        <div className="section-heading"><h2 id="devices-title" data-testid="devices-title">Your devices</h2></div>
        <section className="device-grid" aria-label="Devices">{devices.map((device) => <DeviceCard key={device.id} device={device} api={api}
          onChange={(updated) => setDevices((current) => current.map((item) =>
            item.id === updated.id ? newerDevice(item, updated) : item))} />)}</section>
        {devices.length === 0 && <p className="subtle" data-testid="empty-devices">No devices connected.</p>}
      </section>}
      {accountsPage && accounts.length === 0 && <section className="empty-state">
        <div className="empty-icon"><Icon /></div><h2 data-testid="empty-title">No accounts connected</h2>
      </section>}
    </main>
    {editing !== undefined && <div className="modal-backdrop"><section className="modal" data-testid="account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-title">
      <div className="modal-header"><div><h2 id="account-title">{editing ? 'Reconnect account' : 'Connect an account'}</h2></div>
        <button className="close" aria-label="Close account form" disabled={busy} onClick={() => { setEditing(undefined); setError('') }}>×</button></div>
      {error && <p className="alert error" data-testid="form-error" role="alert">{error}</p>}
      <ConnectionForm key={editing?.id ?? 'new'} account={editing} busy={busy} onSave={saveAccount} />
    </section></div>}
  </div>
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
