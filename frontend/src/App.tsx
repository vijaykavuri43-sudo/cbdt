import { useCallback, useEffect, useState } from 'react'
import { api, type Status } from './lib/api'
import { Gate } from './components/Gate'
import { CommandPalette } from './components/CommandPalette'
import { ErrorBoundary } from './components/ErrorBoundary'
import { StatusPage } from './pages/Status'
import { Explorer } from './pages/Explorer'
import { LiveFeed } from './pages/LiveFeed'
import { Chat } from './pages/Chat'
import { Deposits } from './pages/Deposits'
import { Upis } from './pages/Upis'
import { Wallets } from './pages/Wallets'
import { Evidence } from './pages/Evidence'
import { Search } from './pages/Search'
import { Ask } from './pages/Ask'

const TABS = [
  { id: 'status', label: 'Overview', icon: '◉' },
  { id: 'explorer', label: 'Explorer', icon: '▤' },
  { id: 'chat', label: 'Live Chats', icon: '💬' },
  { id: 'search', label: 'Search', icon: '⌕' },
  { id: 'live', label: 'Live Feed', icon: '⚡' },
  { id: 'deposits', label: 'Mule Money Flows', icon: '₹' },
  { id: 'upis', label: 'UPI / Mules', icon: '@' },
  { id: 'wallets', label: 'Crypto Wallets', icon: '₿' },
  { id: 'evidence', label: 'Evidence', icon: '⛨' },
  { id: 'ask', label: 'AI Investigator', icon: '✦' },
] as const

type TabId = (typeof TABS)[number]['id']

export default function App() {
  const [tab, setTab] = useState<TabId>('status')
  const [status, setStatus] = useState<Status | null>(null)
  const [locked, setLocked] = useState(false)

  const load = useCallback(() => {
    return api<Status>('/api/status')
      .then((s) => {
        setStatus(s)
        setLocked(false)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (locked) return
    load()
    // Poll every 60s — status changes infrequently, reduces VM load
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [locked, load])

  useEffect(() => {
    const h = () => setLocked(true)
    window.addEventListener('s3lm-unauth', h)
    return () => window.removeEventListener('s3lm-unauth', h)
  }, [])

  if (locked)
    return (
      <Gate
        onUnlock={(s) => {
          setStatus(s)
          setLocked(false)
        }}
      />
    )

  return (
    <div className="app">
      <CommandPalette onJump={(id) => setTab(id as TabId)} />
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-title">GCS EVIDENCE MONITOR</div>
          <div className="brand-sub mono">{status?.bucket_url.split('//')[1]?.split('/')[0] ?? '…'}</div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.id}
              className={'nav-item' + (tab === t.id ? ' active' : '')}
              onClick={() => setTab(t.id)}
            >
              <span className="nav-icon">{t.icon}</span>
              {t.label}
              {t.id === 'live' && status && !status.sweep_done && <span className="pulse-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className={`conn ${status?.bucket_live ? 'ok' : 'bad'}`}>
            {status?.bucket_live ? 'BUCKET REACHABLE' : 'BUCKET UNREACHABLE'}
          </div>
          <div className="small dim">{status?.objects_total.toLocaleString() ?? '—'} objects indexed</div>
          <div className="mono tiny dim" style={{ marginTop: 8 }}>
            {status?.case}
          </div>
          <button
            className="btn tiny"
            style={{ marginTop: 10, width: '100%' }}
            onClick={() => {
              localStorage.removeItem('s3lm_token')
              location.reload()
            }}
          >
            Lock console
          </button>
          <div className="tiny dim mono" style={{ marginTop: 10, lineHeight: 1.5, opacity: 0.8 }}>
            ⌘K jump · / search · G gallery · ← → pages
          </div>
        </div>
      </aside>
      <main className="content">
        {/* Keep mounted to avoid refetch on tab switch — just hide */}
        <div style={{ display: tab === 'status' ? 'block' : 'none' }}>
          <ErrorBoundary><StatusPage /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'explorer' ? 'block' : 'none' }}>
          <ErrorBoundary><Explorer prefixes={status?.prefixes ?? []} /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'chat' ? 'block' : 'none' }}>
          <ErrorBoundary><Chat /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'search' ? 'block' : 'none' }}>
          <ErrorBoundary><Search /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'live' ? 'block' : 'none' }}>
          <ErrorBoundary><LiveFeed /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'deposits' ? 'block' : 'none' }}>
          <ErrorBoundary><Deposits /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'upis' ? 'block' : 'none' }}>
          <ErrorBoundary><Upis /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'wallets' ? 'block' : 'none' }}>
          <ErrorBoundary><Wallets /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'evidence' ? 'block' : 'none' }}>
          <ErrorBoundary><Evidence /></ErrorBoundary>
        </div>
        <div style={{ display: tab === 'ask' ? 'block' : 'none' }}>
          <ErrorBoundary><Ask /></ErrorBoundary>
        </div>
      </main>
    </div>
  )
}
