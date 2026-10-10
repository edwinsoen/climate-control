import { useEffect, useId, useRef, useState } from 'react'

export function UserMenu({ identity, onSignOut }: {
  identity: string | null; onSignOut: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const signOut = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const initial = identity?.trim().charAt(0).toLocaleUpperCase() || 'U'

  useEffect(() => {
    if (!open) return
    signOut.current?.focus()
    const closeOutside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [open])

  async function logout() {
    if (busy) return
    setBusy(true)
    try { await onSignOut(); setOpen(false) }
    finally { setBusy(false) }
  }

  return <div className="user-menu" ref={container} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
  }}>
    <button type="button" className="user-avatar" data-testid="user-menu-trigger" ref={trigger}
      aria-label={`User menu for ${identity ?? 'signed-in user'}`} aria-haspopup="menu"
      aria-expanded={open} aria-controls={open ? menuId : undefined} disabled={busy}
      onClick={() => setOpen(!open)} onKeyDown={(event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault(); setOpen(true); signOut.current?.focus()
        }
      }}><span aria-hidden="true">{initial}</span></button>
    {open && <div className="user-dropdown" data-testid="user-menu" id={menuId} role="menu"
      aria-label="User account" onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault(); setOpen(false); trigger.current?.focus()
        } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault(); signOut.current?.focus()
        }
      }}>
      {identity && <p className="user-identity" data-testid="user-identity" role="presentation">{identity}</p>}
      <button type="button" role="menuitem" data-testid="sign-out" ref={signOut}
        disabled={busy} onClick={() => void logout()}>{busy ? 'Signing out…' : 'Sign out'}</button>
    </div>}
  </div>
}
