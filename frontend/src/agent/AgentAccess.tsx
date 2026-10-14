import { useEffect, useState } from 'react'

type Api = <T>(path: string, options?: RequestInit) => Promise<T>
type Token = { id: string; name: string; scopes: string[]; created_at: number; last_used_at: number | null; expires_at: number | null }
type Status = { enabled: boolean; provider: string | null; runtime: string; model: string | null; mcp_url: string; scopes: string[] }

const scopeLabels: Record<string, string> = { read: 'Read', control: 'Control devices', rules: 'Edit rules' }
const providerLabels: Record<string, string> = { openrouter: 'OpenRouter', gemini: 'Gemini', remote: 'Agent Runtime' }

function agentSummary(status: Status) {
  if (!status.enabled || !status.provider) return 'Not configured. Agent-managed devices wait for an external planner.'
  return [providerLabels[status.provider] ?? status.provider, status.model].filter(Boolean).join(' · ')
}

export function AgentAccess({ api }: { api: Api }) {
  const [status, setStatus] = useState<Status>()
  const [tokens, setTokens] = useState<Token[]>([])
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>(['read'])
  const [created, setCreated] = useState('')
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void api<Status>('/api/agent/status').then(setStatus).catch(() => {})
    void api<Token[]>('/api/access-tokens').then(setTokens).catch(() => {})
  }, [api])

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action() } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  function create(event: React.FormEvent) {
    event.preventDefault()
    void perform(async () => {
      const token = await api<Token & { token: string }>('/api/access-tokens', { method: 'POST', body: JSON.stringify({ name, scopes }) })
      setCreated(token.token); setCopied(false); setName('')
      setTokens(await api<Token[]>('/api/access-tokens'))
    })
  }

  function revoke(token: Token) {
    void perform(async () => {
      await api(`/api/access-tokens/${token.id}`, { method: 'DELETE' })
      setTokens(await api<Token[]>('/api/access-tokens'))
    })
  }

  async function copy() {
    try { await navigator.clipboard.writeText(created); setCopied(true) } catch { setCopied(false) }
  }

  return <section className="agent-access" data-testid="agent-access" aria-labelledby="agent-access-title">
    <div className="section-heading"><h2 id="agent-access-title">Agent access</h2></div>
    {status && <p className="subtle" data-testid="agent-status">Rules agent: {agentSummary(status)}</p>}
    {status && <p className="subtle">MCP endpoint <code data-testid="mcp-url">{status.mcp_url}</code></p>}
    {error && <p className="alert error" role="alert">{error}</p>}
    {created && <div className="alert notice agent-token-created" data-testid="token-created" role="status">
      <p>Copy this token now. It will not be shown again.</p>
      <div className="agent-token-copy"><input readOnly value={created} aria-label="New access token" data-testid="token-value" onFocus={e => e.currentTarget.select()} />
        <button type="button" className="button small" data-testid="token-copy" onClick={() => void copy()}>{copied ? 'Copied' : 'Copy'}</button></div>
    </div>}
    {tokens.length > 0 && <div className="account-list" data-testid="token-list">{tokens.map(token => <article className="account-row agent-token-row" data-testid="token-row" key={token.id}>
      <div className="account-description"><h3>{token.name}</h3>
        <p>{token.scopes.map(scope => scopeLabels[scope] ?? scope).join(' · ')}</p>
        <small>{token.last_used_at ? `Last used ${new Date(token.last_used_at * 1000).toLocaleString()}` : 'Never used'}</small></div>
      <button type="button" className="button small danger" data-testid="token-revoke" disabled={busy} onClick={() => revoke(token)}>Revoke</button>
    </article>)}</div>}
    <form className="agent-token-form" data-testid="token-form" onSubmit={create}>
      <label>Token name<input data-testid="token-name" value={name} maxLength={80} required onChange={e => setName(e.target.value)} /></label>
      <fieldset className="automation-days"><legend>Scopes</legend>{(status?.scopes ?? Object.keys(scopeLabels)).map(scope => <label key={scope}>
        <input type="checkbox" data-testid={`token-scope-${scope}`} checked={scopes.includes(scope)}
          onChange={e => setScopes(e.target.checked ? [...scopes, scope] : scopes.filter(item => item !== scope))} />{scopeLabels[scope] ?? scope}</label>)}</fieldset>
      <button className="button primary" data-testid="token-create" disabled={busy || !name.trim() || scopes.length === 0}>Create token</button>
    </form>
  </section>
}
