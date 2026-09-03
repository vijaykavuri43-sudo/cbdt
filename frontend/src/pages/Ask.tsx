import { useState } from 'react'
import { post } from '../lib/api'

interface Src { kind: string; ref: string }
interface Msg { role: 'you' | 'ai'; text: string; sources?: Src[]; model?: string }

const EXAMPLES = [
  'Total deposits vs withdrawals?',
  "Today's deposit total and count?",
  'Top mule rails in the last 7 days?',
  'Deposits above ₹50,000 this month?',
  'OCR coverage and backlog ETA?',
  'Is the full bucket completely parsed?',
]

export function Ask() {
  const [q, setQ] = useState('')
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [busy, setBusy] = useState(false)

  async function send(text?: string) {
    const question = (text ?? q).trim()
    if (!question || busy) return
    setQ('')
    setMsgs((m) => [...m, { role: 'you', text: question }])
    setBusy(true)
    try {
      const d = await post<any>('/api/ask', { question }, 90000)
      setMsgs((m) => [...m, { role: 'ai', text: d.answer ?? '(empty)', sources: d.sources ?? [], model: d.model }])
    } catch (e: any) {
      setMsgs((m) => [...m, { role: 'ai', text: `Error: ${e?.message ?? e}` }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">✦</span>
          <div>
            <h1 className="pg-header-title">AI Investigator</h1>
            <div className="pg-header-sub">Gemini answers over live case data · read-only · totals, UTR/VPA/wallet lookups, image inspection</div>
          </div>
        </div>
      </div>
      <div className="st-panel" style={{ maxWidth: 900 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minHeight: 300, maxHeight: 560, overflowY: 'auto', padding: '4px 2px' }}>
          {msgs.length === 0 && (
            <div className="small dim">
              Ask in plain words — e.g. totals, a 12-digit UTR, a VPA like <code className="mono">name@upi</code>, a wallet address,
              or paste an image object key to inspect it. The assistant reads the live bucket index + payment database.
            </div>
          )}
          {msgs.map((m, i) => (
            <div key={i} style={{
              alignSelf: m.role === 'you' ? 'flex-end' : 'flex-start',
              background: m.role === 'you' ? 'var(--accent-dim, #123)' : 'var(--panel2, #141b2e)',
              borderRadius: 8, padding: '8px 12px', maxWidth: '88%', whiteSpace: 'pre-wrap',
            }}>
              <div style={{ fontSize: 13 }}>{m.text}</div>
              {m.sources && m.sources.length > 0 && (
                <div className="tiny dim" style={{ marginTop: 6 }}>
                  sources: {m.sources.map((s, j) => <code key={j} className="mono" style={{ marginRight: 6 }}>{s.kind}: {s.ref}</code>)}
                </div>
              )}
              {m.model && <div className="tiny dim" style={{ marginTop: 2 }}>{m.model}</div>}
            </div>
          ))}
          {busy && <div className="small dim">✦ thinking over live case data…</div>}
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <input className="input" style={{ flex: 1 }} placeholder="Ask about the case… (Enter to send)"
            value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') send() }} />
          <button className="btn btn-primary" disabled={busy || !q.trim()} onClick={() => send()}>
            {busy ? '…' : 'Ask'}
          </button>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
          {EXAMPLES.map((e) => (
            <button key={e} className="btn" style={{ fontSize: 11 }} disabled={busy} onClick={() => send(e)}>{e}</button>
          ))}
        </div>
      </div>
    </>
  )
}
