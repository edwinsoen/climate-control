import { useEffect, useState } from 'react'
import { ago } from '../format'

type Api = <T>(path: string, options?: RequestInit) => Promise<T>
type Token = { id: string; name: string; scopes: string[]; created_at: number; last_used_at: number | null; expires_at: number | null }
export type AgentStatus = { enabled: boolean; provider: string | null; runtime: string; model: string | null; mcp_url: string; scopes: string[] }

const scopeLabels: Record<string, string> = { read: 'Read', control: 'Control devices', rules: 'Edit rules' }
export function AgentAccess({ api, status }: { api: Api; status: AgentStatus | undefined }) {
  const [tokens, setTokens] = useState<Token[]>([])
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>(['read'])
  const [created, setCreated] = useState('')
  const [copied, setCopied] = useState<'url' | 'token' | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
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
      setCreated(token.token); setCopied(null); setName('')
      setTokens(await api<Token[]>('/api/access-tokens'))
    })
  }

  function revoke(token: Token) {
    void perform(async () => {
      await api(`/api/access-tokens/${token.id}`, { method: 'DELETE' })
      setTokens(await api<Token[]>('/api/access-tokens'))
    })
  }

  async function copy(value: string, which: 'url' | 'token') {
    try { await navigator.clipboard.writeText(value); setCopied(which) } catch { setCopied(null) }
  }

  const mcpUrl = status?.mcp_url
  return <section id="agents" className="settings-section flush" data-testid="agent-access" aria-labelledby="agent-access-title">
    <div className="card-header">
      <div><h2 id="agent-access-title">Agent access</h2><p className="subtle">MCP access tokens. Shown once on creation.</p></div>
    </div>
    <div className="section-body form-stack">
      {mcpUrl && <label>MCP endpoint
        <span className="copy-field"><input readOnly value={mcpUrl} data-testid="mcp-url" onFocus={e => e.currentTarget.select()} />
          <button type="button" className="button" data-testid="mcp-url-copy" onClick={() => void copy(mcpUrl, 'url')}>{copied === 'url' ? 'Copied' : 'Copy'}</button></span>
      </label>}
      {error && <p className="alert error" role="alert">{error}</p>}
      {created && <div className="alert notice form-stack" data-testid="token-created" role="status">
        <p>Copy this token now. It will not be shown again.</p>
        <span className="copy-field"><input readOnly value={created} aria-label="New access token" data-testid="token-value" onFocus={e => e.currentTarget.select()} />
          <button type="button" className="button" data-testid="token-copy" onClick={() => void copy(created, 'token')}>{copied === 'token' ? 'Copied' : 'Copy'}</button></span>
      </div>}
    </div>
    {tokens.length > 0 && <div className="table-wrap"><table className="table" data-testid="token-list">
      <thead><tr><th scope="col">Name</th><th scope="col">Scopes</th><th scope="col">Last used</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
      <tbody>{tokens.map(token => <tr data-testid="token-row" key={token.id}>
        <td><strong>{token.name}</strong></td>
        <td><span className="scope-list">{token.scopes.map(scope => <span key={scope}>{scopeLabels[scope] ?? scope}</span>)}</span></td>
        <td title={token.last_used_at ? new Date(token.last_used_at * 1000).toLocaleString() : undefined}>{token.last_used_at ? ago(token.last_used_at) : 'Never used'}</td>
        <td className="actions-cell"><button type="button" className="button small ghost danger" data-testid="token-revoke" disabled={busy} onClick={() => revoke(token)}>Revoke</button></td>
      </tr>)}</tbody>
    </table></div>}
    <div className="section-body">
      <form className="token-form" data-testid="token-form" onSubmit={create}>
        <h3>Create token</h3>
        <label>Token name<input data-testid="token-name" value={name} maxLength={80} required onChange={e => setName(e.target.value)} placeholder="e.g. Claude Code · laptop" /></label>
        <fieldset className="check-group"><legend>Scopes</legend>{(status?.scopes ?? Object.keys(scopeLabels)).map(scope => <label className="check-row" key={scope}>
          <input type="checkbox" data-testid={`token-scope-${scope}`} checked={scopes.includes(scope)}
            onChange={e => setScopes(e.target.checked ? [...scopes, scope] : scopes.filter(item => item !== scope))} />{scopeLabels[scope] ?? scope}</label>)}</fieldset>
        <button className="button primary" data-testid="token-create" disabled={busy || !name.trim() || scopes.length === 0}>Create token</button>
      </form>
    </div>
  </section>
}
