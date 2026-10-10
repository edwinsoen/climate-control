import { useState } from 'react'
import { availableProviders, providers } from './providers'
import type { Account, ProviderId } from './types'

export type AccountConnection = { provider: ProviderId; label: string } & Record<string, unknown>

export function ConnectionForm({ account, busy, onSave }: {
  account: Account | null
  busy: boolean
  onSave: (connection: AccountConnection) => Promise<void>
}) {
  const [providerId, setProviderId] = useState<ProviderId>(account?.provider ?? availableProviders[0].id)
  const provider = providers[providerId]
  const Fields = provider.Fields

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    await onSave({
      ...provider.credentials(form),
      provider: provider.id,
      label: String(form.get('label') ?? ''),
    })
  }

  return <form onSubmit={submit}>
    <label>Provider<select data-testid="connection-provider" value={providerId} disabled={busy || !!account} onChange={(event) => setProviderId(event.target.value as ProviderId)}>
      {availableProviders.map(({ id, name }) => <option key={id} value={id}>{name}</option>)}
    </select></label>
    <label>Account label<input data-testid="connection-label" name="label" autoFocus required maxLength={100} defaultValue={account?.label} placeholder="Home" /></label>
    <Fields key={providerId} account={account} />
    <button className="button primary full" data-testid="connection-submit" disabled={busy}>{busy ? 'Connecting…' : account ? 'Save & reconnect' : 'Connect account'}</button>
  </form>
}
