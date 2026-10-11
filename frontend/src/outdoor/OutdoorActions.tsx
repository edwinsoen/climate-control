import { useEffect, useId, useRef, useState } from 'react'

export function OutdoorActions({ busy, canChange, onChangeLocation, onRemove }: {
  busy: boolean; canChange: boolean; onChangeLocation: () => void; onRemove: () => void
}) {
  const [open, setOpen] = useState(false)
  const actions = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const focusLast = useRef(false)
  const menuId = useId()

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

  return <div className="account-actions" ref={actions} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
  }}>
    <button type="button" className="account-menu-trigger" data-testid="outdoor-actions" ref={trigger}
      aria-label="Outdoor location actions" disabled={busy}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
      onClick={() => { focusLast.current = false; setOpen(!open) }} onKeyDown={(event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault(); focusLast.current = event.key === 'ArrowUp'; setOpen(true)
        }
      }}>
      <svg className="account-menu-icon" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="5" cy="12" r="1.7" /><circle cx="12" cy="12" r="1.7" /><circle cx="19" cy="12" r="1.7" />
      </svg>
    </button>
    {open && <div className="account-menu" data-testid="outdoor-menu" id={menuId}
      role="menu" aria-label="Outdoor location actions" ref={menu} onKeyDown={(event) => {
        if (event.key === 'Escape') { event.preventDefault(); close() }
        else if (event.key === 'Tab') close()
        else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])
          const index = items.indexOf(document.activeElement as HTMLButtonElement)
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
            : (index + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length
          event.preventDefault(); items[next]?.focus()
        }
      }}>
      <button type="button" role="menuitem" data-testid="outdoor-change-location" disabled={busy || !canChange}
        onClick={() => { close(); onChangeLocation() }}>Change location</button>
      <button type="button" role="menuitem" className="danger" data-testid="outdoor-remove-location"
        disabled={busy} onClick={() => { close(); onRemove() }}>Remove location</button>
    </div>}
  </div>
}
