import { useCallback, useEffect, useState } from 'react'
import { api, clearToken, type Status } from './lib/api'
import { clearApiCache, readApiCache, writeApiCache } from './lib/cache'
import { Gate } from './components/Gate'
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
import { Agents } from './pages/Agents'
import { Ask } from './pages/Ask'
import { OfficerQuery } from './pages/OfficerQuery'

interface BootConfig {
  source_label?: string
  peers?: { label: string; port: number }[]
  prefixes?: string[]
  bucket_url?: string
  case?: string
  auth_ref?: string
}

declare global {
  interface Window {
    /** Injected by the backend's SPA fallback — available before any fetch. */
    __S3LM_BOOT__?: BootConfig
  }
}

const EMPTY_BOOT: BootConfig = {}

function readBoot(): BootConfig {
  try {
    return window.__S3LM_BOOT__ ?? EMPTY_BOOT
  } catch {
    return EMPTY_BOOT
  }
}

type TabId =
  | 'status'
  | 'explorer'
  | 'live'
  | 'chat'
  | 'officer'
  | 'deposits'
  | 'cashflow'
  | 'ledger'
  | 'agents'
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
    label: 'People',
    tabs: [
      { id: 'agents', label: 'Agents & operators', icon: '☖' },
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
  // Paint the last known status before the network answers; the boot config
  // (injected into index.html) keeps the Sources group visible even on the
  // very first visit, so the sidebar never blanks while switching buckets.
  const [boot] = useState<BootConfig>(readBoot)
  const [status, setStatus] = useState<Status | null>(
    () => readApiCache<Status>('/api/status')?.v ?? null,
  )
  const [locked, setLocked] = useState(false)
  const peers = boot.peers?.length ? boot.peers : (status?.peers ?? [])
  const currentLabel = boot.source_label || status?.source_label || ''

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
        writeApiCache('/api/status', undefined, s)
        setLocked(false)
      })
      .catch(() => {})
  }, [])

  const hasStatus = !!status
  useEffect(() => {
    if (locked) return
    load()
    // Retry faster until the first status arrives (cold backend / deploy):
    // waiting a full minute left the sidebar and Overview blank.
    const t = setInterval(load, hasStatus ? 60000 : 8000)
    return () => clearInterval(t)
  }, [locked, load, hasStatus])

  useEffect(() => {
    const h = () => {
      // A 401 means the session is dead — drop cached case data so it cannot
      // be read from localStorage while the console is locked.
      clearApiCache()
      setLocked(true)
    }
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
          writeApiCache('/api/status', undefined, s)
          setLocked(false)
        }}
      />
    )

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-title">GCS EVIDENCE MONITOR</div>
        </div>
        {peers.length > 0 && (
          <div className="nav-group">
            <div className="nav-group-label">Sources</div>
            {peers.map((p) => {
              const here = currentLabel === p.label
              const href = `${window.location.protocol}//${window.location.hostname}:${p.port}/`
              return here ? (
                <div key={p.label} className="nav-item active mono" style={{ fontSize: 11 }}>
                  <span className="nav-icon">◉</span>
                  <span className="nav-label">{p.label}</span>
                </div>
              ) : (
                <a key={p.label} className="nav-item mono" style={{ fontSize: 11 }} href={href}>
                  <span className="nav-icon">○</span>
                  <span className="nav-label">
                    {p.label} <span className="dim">:{p.port}</span>
                  </span>
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
                  <span className="nav-label">{t.label}</span>
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
          <button
            className="btn tiny"
            style={{ width: '100%' }}
            onClick={() => {
              clearToken()
              clearApiCache()
              location.reload()
            }}
          >
            Lock console
          </button>
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
            <ErrorBoundary><Explorer prefixes={status?.prefixes ?? boot.prefixes ?? []} jumpTo={exploreJump} /></ErrorBoundary>
          </div>
        )}
        {visited.has('agents') && (
          <div style={{ display: tab === 'agents' ? 'block' : 'none' }}>
            <Agents />
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
