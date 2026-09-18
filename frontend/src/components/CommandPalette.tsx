import { useEffect, useState } from 'react'

export function CommandPalette({ onJump }: { onJump: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const tabs = [
    { id: 'status', label: 'Overview', hint: 'Briefing, pipeline, folders' },
    { id: 'explorer', label: 'Explorer', hint: 'Folders + search name or path' },
    { id: 'chat', label: 'Live Chats', hint: 'Conversations + voice notes' },
    { id: 'live', label: 'Live Feed', hint: 'Real-time uploads' },
    { id: 'officer', label: 'Date range', hint: 'All folders or one + UPI/bank/wallet CSV' },
    { id: 'deposits', label: 'Mule rails', hint: 'Unique UPI / bank / wallet freeze list' },
    { id: 'cashflow', label: 'Cash flow', hint: 'Operator ledger, ex-bonus' },
    { id: 'ledger', label: 'Ledger', hint: 'XLSX / CSV books' },
    { id: 'upis', label: 'UPI / VPAs', hint: 'QR-decoded unique collection VPAs' },
    { id: 'wallets', label: 'Crypto wallets', hint: 'Unique addresses + CSV' },
    { id: 'ask', label: 'AI Investigator', hint: 'Ask over the whole bucket' },
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
