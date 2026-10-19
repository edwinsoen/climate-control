import { StrictMode, useCallback, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import { ConnectionForm, type AccountConnection } from './accounts/ConnectionForm'
import type { Account } from './accounts/types'
import { BrandLogo, Icon } from './components/Icon'
import { AutomationPage } from './devices/AutomationPage'
import { DevicePage } from './devices/DevicePage'
import { newerDevice, type Device } from './devices/types'
import { HomePage } from './home/HomePage'
import type { Outdoor } from './outdoor/OutdoorCard'
import { SettingsPage } from './settings/SettingsPage'
import { UserMenu } from './UserMenu'
import './styles.css'

type Session = {
  authenticated: boolean; identity: string | null; csrf_token: string | null
  google_enabled: boolean; local_enabled: boolean
}

function App() {
  const [session, setSession] = useState<Session | null>(null)
  const sessionRef = useRef<Session | null>(null)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [devices, setDevices] = useState<Device[]>([])
  const [loaded, setLoaded] = useState(false)
  const [outdoor, setOutdoor] = useState<Outdoor | null>(null)
  const outdoorRevision = useRef(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<Account | null | undefined>(undefined)
  const route = currentRoute()

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
    const revision = outdoorRevision.current
    const [nextAccounts, nextDevices, nextOutdoor] = await Promise.all([
      api<Account[]>('/api/accounts'), api<Device[]>('/api/devices'),
      api<Outdoor>('/api/outdoor'),
    ])
    setAccounts(nextAccounts)
    if (revision === outdoorRevision.current) setOutdoor(nextOutdoor)
    setDevices((current) => nextDevices.map((incoming) => newerDevice(current.find((item) => item.id === incoming.id), incoming)))
    setLoaded(true)
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
    if (!session?.authenticated || !outdoor?.loading) return
    const locationId = outdoor.location_id
    const controller = new AbortController()
    let pending = false
    const reload = async () => {
      if (pending || document.hidden) return
      pending = true
      const revision = outdoorRevision.current
      try {
        const next = await api<Outdoor>('/api/outdoor', { signal: controller.signal })
        if (!controller.signal.aborted && revision === outdoorRevision.current && next.location_id === locationId) {
          outdoorRevision.current++
          setOutdoor(next)
        }
      } catch (err) { if (!controller.signal.aborted) setError((err as Error).message) }
      finally { pending = false }
    }
    void reload()
    const timer = window.setInterval(() => { void reload() }, 1000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [api, session?.authenticated, outdoor?.loading, outdoor?.location_id])

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

  async function saveAccount(connection: AccountConnection, existing = editing) {
    setBusy(true); setError(''); setNotice('')
    try {
      const account = await api<Account>(existing ? `/api/accounts/${existing.id}` : '/api/accounts', {
        method: existing ? 'PUT' : 'POST', body: JSON.stringify(connection),
      })
      await load()
      if (account.error?.includes('verification code')) {
        setEditing(account)
        setError(account.error)
        return
      }
      setEditing(undefined)
      setNotice(account.error ?? (account.status === 'pending' ? 'Connecting account…' : 'Account connected.'))
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  async function remove(account: Account) {
    setError('')
    try { await api(`/api/accounts/${account.id}`, { method: 'DELETE' }); await load(); return true }
    catch (err) { setError((err as Error).message); return false }
  }

  const changeDevice = (updated: Device) => setDevices((current) => current.map((item) =>
    item.id === updated.id ? newerDevice(item, updated) : item))
  const changeOutdoor = (next: Outdoor) => { outdoorRevision.current++; setOutdoor(next) }
  const brand = <a className="brand" href="/" data-testid="nav-home"><BrandLogo /><span>Climate Control</span></a>
  if (loading) return <main className="loading" aria-live="polite">Loading Climate Control…</main>
  if (!session?.authenticated) return <main className="login-layout">
    <div className="login-box">
      <div className="login-heading"><BrandLogo /><h1 data-testid="login-title">Sign in to Climate Control</h1></div>
      {error && <p className="alert error" data-testid="form-error" role="alert">{error}</p>}
      {new URLSearchParams(window.location.search).get('error') === 'google' && <p className="alert error" role="alert">Google sign-in could not be completed. Use an allowed account or try again.</p>}
      {session?.google_enabled && <a className="button full" href="/auth/google/login">Continue with Google</a>}
      {session?.google_enabled && session.local_enabled && <p className="divider">or as administrator</p>}
      {session?.local_enabled && <form className="login-form" onSubmit={login}>
        <label>Username<input name="username" autoComplete="username" required defaultValue="admin" /></label>
        <label>Password<input data-testid="login-password" name="password" type="password" autoComplete="current-password" required /></label>
        <button className="button primary full" data-testid="login-submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>}
      {!session && <button className="button" onClick={() => window.location.reload()}>Try again</button>}
      <p className="login-note">Private household app. Only approved accounts can sign in.</p>
    </div>
  </main>

  const device = route.deviceId ? devices.find((item) => item.id === route.deviceId) : undefined
  return <div className="app-shell">
    <header className="app-header" data-testid="app-header"><div className="app-header-inner">{brand}
      <a className="header-link" data-testid="nav-settings" href="/settings" aria-label="Settings" aria-current={route.page === 'settings' ? 'page' : undefined}><Icon name="settings" large /></a>
      <UserMenu identity={session.identity} onSignOut={async () => {
        try { await api('/api/logout', { method: 'POST' }); updateSession(await api<Session>('/api/session')) }
        catch (err) { setError((err as Error).message) }
      }} />
    </div></header>
    {error && editing === undefined && route.page !== 'home' && <div className="page-alert"><p className="alert error" data-testid="form-error" role="alert">{error}</p></div>}
    {route.page === 'home' && <>
      {error && editing === undefined && (devices.length > 0 || accounts.length > 0) && <div className="page-alert"><p className="alert error" data-testid="form-error" role="alert">{error}</p></div>}
      <HomePage api={api} accounts={accounts} devices={devices} outdoor={outdoor} busy={busy} notice={notice} connectError={editing === undefined && !accounts.length ? error : ''}
        onConnect={(connection) => saveAccount(connection, null)} onDeviceChange={changeDevice} onOutdoorChange={changeOutdoor} onRefresh={load} />
    </>}
    {route.page === 'device' && device && <DevicePage key={device.id} device={device} devices={devices} api={api} onChange={changeDevice} />}
    {route.page === 'device' && !device && <main className="page"><p className="subtle" role="status">{loaded ? 'This purifier is not connected.' : 'Loading purifier…'}</p></main>}
    {route.page === 'rules' && <AutomationPage deviceId={route.deviceId!} api={api} />}
    {route.page === 'settings' && <SettingsPage api={api} accounts={accounts} devices={devices} outdoor={outdoor} notice={notice}
      onConnect={() => { setError(''); setEditing(null) }} onReconnect={(account) => { setError(''); setEditing(account) }}
      onRemove={remove} onOutdoorChange={changeOutdoor} />}
    {editing !== undefined && <div className="modal-backdrop"><section className="modal" data-testid="account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-title">
      <div className="modal-header"><h2 id="account-title">{editing ? 'Reconnect account' : 'Connect an account'}</h2>
        <button className="icon-button bare" aria-label="Close account form" disabled={busy} onClick={() => { setEditing(undefined); setError('') }}><Icon name="close" /></button></div>
      {error && <p className="alert error" data-testid="form-error" role="alert">{error}</p>}
      <ConnectionForm key={editing?.id ?? 'new'} account={editing} busy={busy} onSave={saveAccount} />
    </section></div>}
  </div>
}

type Route = { page: 'home' | 'device' | 'rules' | 'settings'; deviceId?: string }

function currentRoute(): Route {
  const path = window.location.pathname
  if (/^\/settings\/?$/.test(path)) return { page: 'settings' }
  const match = /^\/devices\/([^/]+)(\/rules)?\/?$/.exec(path)
  if (match) return { page: match[2] ? 'rules' : 'device', deviceId: decodeURIComponent(match[1]) }
  return { page: 'home' }
}

// The accounts page became a Settings section.
if (window.location.pathname === '/accounts') window.history.replaceState(null, '', '/settings#accounts')

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
