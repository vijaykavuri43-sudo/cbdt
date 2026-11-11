import { useState } from 'react'
import { api, clearToken, getToken, setToken, hasToken, type Status } from '../lib/api'

export function Gate({ onUnlock }: { onUnlock: (s: Status) => void }) {
  const [val, setVal] = useState(getToken())
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function tryUnlock() {
    setErr('')
    if (!val.trim()) return setErr('Enter the access key.')
    setBusy(true)
    try {
      setToken(val)
      const s = await api<Status>('/api/status')
      onUnlock(s)
    } catch (e) {
      clearToken()
      setErr(e instanceof Error && e.message === 'locked' ? 'Access key rejected.' : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate-wrap">
      <div className="gate-card">
        <div className="brand-title" style={{ marginBottom: 4 }}>
          GCS EVIDENCE MONITOR
        </div>
        <div className="mono tiny dim" style={{ marginBottom: 18 }}>
          CASE-2026-CBTF · RESTRICTED EVIDENCE CONSOLE
        </div>
        <input
          type="password"
          autoFocus
          placeholder="Access key"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && tryUnlock()}
        />
        <button className="btn btn-primary" style={{ width: '100%', marginTop: 10 }} disabled={busy} onClick={tryUnlock}>
          {busy ? 'Verifying…' : 'Unlock console'}
        </button>
        {err && <div style={{ color: 'var(--bad)', fontSize: 12, marginTop: 10 }}>{err}</div>}
        <div className="tiny dim mono" style={{ marginTop: 18, lineHeight: 1.6 }}>
          Access key location on the case machine:
          <br />
          <code>~/.local/share/s3-live-monitor/access-token.txt</code>
          <br />
          or set S3LM_TOKEN when starting the backend.
        </div>
        {hasToken() && (
          <button
            className="btn"
            style={{ width: '100%', marginTop: 14 }}
            onClick={() => {
              setToken(val)
              window.location.reload()
            }}
          >
            Retry with stored key
          </button>
        )}
      </div>
    </div>
  )
}
