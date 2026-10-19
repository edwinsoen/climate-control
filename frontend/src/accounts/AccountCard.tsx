import { useEffect, useId, useRef, useState } from 'react'
import { MoreIcon } from '../components/Icon'
import { getProvider } from './providers'
import type { Account } from './types'

export function accountStatus(account: Account): string {
  return ({ connected: 'Connected', pending: 'Connecting', auth_error: 'Reconnect needed',
    rate_limited: 'Cooling down', unavailable: 'Unavailable' })[account.status] ?? 'Pending'
}

export function accountBand(account: Account) {
  return account.status === 'connected' ? 'band-good' : account.status === 'auth_error' ? 'band-unhealthy' : 'band-moderate'
}

export function AccountCard({ account, purifiers, onReconnect, onRemove }: {
  account: Account
  purifiers: string[]
  onReconnect: (account: Account) => void
  onRemove: (account: Account) => Promise<boolean>
}) {
  const provider = getProvider(account.provider)
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [removing, setRemoving] = useState(false)
  const cancel = useRef<HTMLButtonElement>(null)
  const actions = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const focusLast = useRef(false)
  const menuId = useId()
  const confirmationId = useId()

  useEffect(() => {
    if (confirming) cancel.current?.focus()
  }, [confirming])

  function cancelRemoval() {
    setConfirming(false)
    trigger.current?.focus()
  }

  async function confirmRemoval() {
    setRemoving(true)
    try {
      if (await onRemove(account)) setConfirming(false)
    } finally { setRemoving(false) }
  }

  useEffect(() => {
    if (!open) return
    const items = menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
    items?.[focusLast.current ? items.length - 1 : 0]?.focus()
    const closeOutside = (event: PointerEvent) => {
      if (!actions.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [open])

  function close() {
    setOpen(false)
    trigger.current?.focus()
  }

  function menuKey(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') { event.preventDefault(); close(); return }
    if (event.key === 'Tab') { close(); return }
    const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    let next: number
    if (event.key === 'ArrowDown') next = (index + 1) % items.length
    else if (event.key === 'ArrowUp') next = (index + items.length - 1) % items.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = items.length - 1
    else return
    event.preventDefault()
    items[next]?.focus()
  }

  return <tbody data-testid="account-card" onKeyDown={(event) => {
    if (confirming && !removing && event.key === 'Escape') {
      event.preventDefault(); cancelRemoval()
    }
  }}>
    <tr>
      <td><div className="cell-stack">
        <strong data-testid="account-name">{account.label}</strong>
        <span data-testid="account-provider">{provider ? `${provider.name} · ${provider.detail}` : 'Unsupported provider'}</span>
      </div></td>
      <td>{provider && account.country_code ? provider.region(account) : 'Not set'}</td>
      <td>{purifiers.length ? purifiers.join(', ') : <span className="hint">None found</span>}</td>
      <td><div className="cell-stack">
        <span className={`band ${accountBand(account)}`}>{accountStatus(account)}</span>
        {account.error && <span className="inline-error">{account.error}</span>}
      </div></td>
      <td className="actions-cell"><div className="account-actions" ref={actions} onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
      }}>
        <button type="button" className="icon-button bare" data-testid="account-actions" ref={trigger} aria-label={`Actions for ${account.label}`}
          disabled={removing}
          aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
          onClick={() => { setConfirming(false); focusLast.current = false; setOpen(!open) }}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
              event.preventDefault(); setConfirming(false); focusLast.current = event.key === 'ArrowUp'; setOpen(true)
            }
          }}><MoreIcon /></button>
        {open && <div className="menu" data-testid="account-menu" id={menuId} role="menu" aria-label={`Actions for ${account.label}`} ref={menu} onKeyDown={menuKey}>
          <button type="button" role="menuitem" data-testid="account-reconnect" disabled={!provider} onClick={() => { close(); onReconnect(account) }}>Reconnect</button>
          <button type="button" role="menuitem" data-testid="account-remove" className="danger" onClick={() => { close(); setConfirming(true) }}>Remove</button>
        </div>}
      </div></td>
    </tr>
    {confirming && <tr className="account-remove-confirmation"><td colSpan={5}>
      <div className="confirm" data-testid="account-remove-confirmation" role="group" aria-labelledby={confirmationId} aria-busy={removing}>
        <p id={confirmationId}>Remove {account.label} and its saved credentials?</p>
        <div className="actions">
          <button type="button" className="button small" data-testid="account-cancel-remove" ref={cancel} disabled={removing} onClick={cancelRemoval}>Cancel</button>
          <button type="button" className="button small danger" data-testid="account-confirm-remove" disabled={removing} onClick={() => void confirmRemoval()}>{removing ? 'Removing…' : 'Remove account'}</button>
        </div>
      </div>
    </td></tr>}
  </tbody>
}
