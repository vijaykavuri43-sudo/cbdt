import { useEffect, useState } from 'react'

export function CommandPalette({ onJump }: { onJump: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const tabs = [
    { id: 'status', label: 'Overview', hint: 'Cases, bucket health, inventory' },
    { id: 'explorer', label: 'Explorer', hint: 'Browse all buckets folders' },
    { id: 'chat', label: 'Live Chats', hint: 'Conversations + voice notes' },
    { id: 'search', label: 'Search', hint: 'Find across 1M keys' },
    { id: 'live', label: 'Live Feed', hint: 'Real-time uploads' },
    { id: 'deposits', label: 'Mule Money Flows', hint: 'Deposits + withdrawals' },
    { id: 'upis', label: 'UPI / Mules', hint: 'QR decoded VPAs' },
    { id: 'evidence', label: 'Evidence', hint: 'Downloads + chain of custody' },
  ]
  const filtered = q
    ? tabs.filter((t) => (t.label + t.hint).toLowerCase().includes(q.toLowerCase()))
    : tabs

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((o) => !o)
      } else if (e.key === 'Escape' && open) setOpen(false)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open])

  if (!open) return null
  return (
    <div className="cmd-backdrop" onClick={() => setOpen(false)}>
      <div className="cmd-box" onClick={(e) => e.stopPropagation()}>
        <input autoFocus placeholder="Jump to…  (type to filter)" value={q} onChange={(e) => setQ(e.target.value)} className="cmd-input" />
        <div className="cmd-list">
          {filtered.map((t) => (
            <button
              key={t.id}
              onClick={() => {
                onJump(t.id)
                setOpen(false)
                setQ('')
              }}
            >
              <span>{t.label}</span>
              <span className="tiny dim">{t.hint}</span>
            </button>
          ))}
          {filtered.length === 0 && <div className="small dim" style={{ padding: 14, textAlign: 'center' }}>No match.</div>}
        </div>
      </div>
    </div>
  )
}
