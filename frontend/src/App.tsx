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
import { Ledger } from './pages/Ledger'
import { Cashflow } from './pages/Cashflow'
import { Upis } from './pages/Upis'
import { Wallets } from './pages/Wallets'
import { Ask } from './pages/Ask'
import { OfficerQuery } from './pages/OfficerQuery'

type TabId =
  | 'status'
  | 'explorer'
  | 'live'
  | 'chat'
  | 'officer'
  | 'deposits'
  | 'cashflow'
  | 'ledger'
  | 'upis'
  | 'wallets'
  | 'ask'

const NAV: { label: string; tabs: { id: TabId; label: string; icon: string }[] }[] = [
  {
    label: 'Monitor',
    tabs: [
      { id: 'status', label: 'Overview', icon: '◉' },
      { id: 'explorer', label: 'Explorer', icon: '▤' },
      { id: 'live', label: 'Live Feed', icon: '⚡' },
      { id: 'chat', label: 'Live Chats', icon: '💬' },
    ],
  },
  {
    label: 'Query',
    tabs: [
      { id: 'officer', label: 'Date range', icon: '⬇' },
    ],
  },
  {
    label: 'Money',
    tabs: [
      { id: 'deposits', label: 'Mule rails', icon: '₹' },
      { id: 'cashflow', label: 'Cash flow', icon: '⇄' },
      { id: 'ledger', label: 'Ledger', icon: '▦' },
    ],
  },
  {
    label: 'Rails',
    tabs: [
      { id: 'upis', label: 'UPI / VPAs', icon: '@' },
      { id: 'wallets', label: 'Crypto wallets', icon: '₿' },
    ],
  },
  {
    label: 'Case',
    tabs: [
      { id: 'ask', label: 'AI Investigator', icon: '✦' },
    ],
  },
]

export default function App() {
  const [tab, setTab] = useState<TabId>('status')
  const [visited, setVisited] = useState<Set<TabId>>(() => new Set(['status']))
  const [exploreJump, setExploreJump] = useState<{ p: string; t: number } | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [locked, setLocked] = useState(false)

  const openTab = useCallback((id: TabId, prefix?: string) => {
    setTab(id)
    setVisited((s) => {
      if (s.has(id)) return s
      const n = new Set(s)
      n.add(id)
      return n
    })
    if (id === 'explorer' && prefix !== undefined) setExploreJump({ p: prefix, t: Date.now() })
  }, [])

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
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [locked, load])

  useEffect(() => {
    const h = () => setLocked(true)
    window.addEventListener('s3lm-unauth', h)
    return () => window.removeEventListener('s3lm-unauth', h)
  }, [])

  useEffect(() => {
    const h = (e: Event) => {
      const d = (e as CustomEvent).detail as { tab?: TabId; prefix?: string } | undefined
      if (d?.tab) openTab(d.tab, d.prefix)
    }
    window.addEventListener('s3lm-goto', h)
    return () => window.removeEventListener('s3lm-goto', h)
  }, [openTab])

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
      <CommandPalette onJump={(id) => openTab(id as TabId)} />
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-title">GCS EVIDENCE MONITOR</div>
          <div className="brand-sub mono">{status?.source_label ?? status?.bucket_url.split('//')[1]?.split('/')[0] ?? '…'}</div>
        </div>
        {status?.peers && status.peers.length > 0 && (
          <div className="nav-group">
            <div className="nav-group-label">Sources</div>
            {status.peers.map((p) => {
              const here = (status.source_label ?? '') === p.label
              const href = `${window.location.protocol}//${window.location.hostname}:${p.port}/`
              return here ? (
                <div key={p.label} className="nav-item active mono" style={{ fontSize: 12 }}>
                  <span className="nav-icon">◉</span>
                  {p.label}
                </div>
              ) : (
                <a key={p.label} className="nav-item mono" style={{ fontSize: 12 }} href={href}>
                  <span className="nav-icon">○</span>
                  {p.label} <span className="dim">:{p.port}</span>
                </a>
              )
            })}
          </div>
        )}
        <nav>
          {NAV.map((g) => (
            <div key={g.label} className="nav-group">
              <div className="nav-group-label">{g.label}</div>
              {g.tabs.map((t) => (
                <button
                  key={t.id}
                  className={'nav-item' + (tab === t.id ? ' active' : '')}
                  onClick={() => openTab(t.id)}
                >
                  <span className="nav-icon">{t.icon}</span>
                  {t.label}
                  {t.id === 'live' && status && !status.sweep_done && <span className="pulse-dot" />}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className={`conn ${status?.bucket_live ? 'ok' : (status && status.bucket_live === false ? 'bad' : (status ? 'ok' : ''))}`}>
            {status?.bucket_live ? 'BUCKET REACHABLE' : (status && status.bucket_live === false ? 'BUCKET UNREACHABLE' : (status ? 'GCS MIRROR' : 'CHECKING…'))}
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
            Ctrl+K jump · / search in Explorer
          </div>
        </div>
      </aside>
      <main className={'content' + (tab === 'explorer' ? ' content-explorer' : '')}>
        {visited.has('status') && (
          <div style={{ display: tab === 'status' ? 'block' : 'none' }}>
            <ErrorBoundary><StatusPage /></ErrorBoundary>
          </div>
        )}
        {visited.has('explorer') && (
          <div className={tab === 'explorer' ? 'pane-fill' : 'pane-hidden'}>
            <ErrorBoundary><Explorer prefixes={status?.prefixes ?? []} jumpTo={exploreJump} /></ErrorBoundary>
          </div>
        )}
        {visited.has('chat') && (
          <div style={{ display: tab === 'chat' ? 'block' : 'none' }}>
            <ErrorBoundary><Chat /></ErrorBoundary>
          </div>
        )}
        {visited.has('live') && (
          <div style={{ display: tab === 'live' ? 'block' : 'none' }}>
            <ErrorBoundary><LiveFeed /></ErrorBoundary>
          </div>
        )}
        {visited.has('officer') && (
          <div style={{ display: tab === 'officer' ? 'block' : 'none' }}>
            <ErrorBoundary><OfficerQuery /></ErrorBoundary>
          </div>
        )}
        {visited.has('deposits') && (
          <div style={{ display: tab === 'deposits' ? 'block' : 'none' }}>
            <ErrorBoundary><Deposits /></ErrorBoundary>
          </div>
        )}
        {visited.has('ledger') && (
          <div style={{ display: tab === 'ledger' ? 'block' : 'none' }}>
            <ErrorBoundary><Ledger /></ErrorBoundary>
          </div>
        )}
        {visited.has('cashflow') && (
          <div style={{ display: tab === 'cashflow' ? 'block' : 'none' }}>
            <ErrorBoundary><Cashflow /></ErrorBoundary>
          </div>
        )}
        {visited.has('upis') && (
          <div style={{ display: tab === 'upis' ? 'block' : 'none' }}>
            <ErrorBoundary><Upis /></ErrorBoundary>
          </div>
        )}
        {visited.has('wallets') && (
          <div style={{ display: tab === 'wallets' ? 'block' : 'none' }}>
            <ErrorBoundary><Wallets /></ErrorBoundary>
          </div>
        )}
        {visited.has('ask') && (
          <div style={{ display: tab === 'ask' ? 'block' : 'none' }}>
            <ErrorBoundary><Ask /></ErrorBoundary>
          </div>
        )}
      </main>
    </div>
  )
}
