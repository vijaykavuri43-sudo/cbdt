import { useEffect, useRef, useState } from 'react'
import { api, eventsUrl, objectUrl } from '../lib/api'
import { readApiCache, writeApiCache } from '../lib/cache'
import { arkhamAddressUrl, bestTronscanUrl, traceDeskAddressUrl, tronscanLabel } from '../lib/explorers'
import { fileColor, fileIcon, fmtBoth, fmtBytes, fmtIST, kindOf } from '../lib/format'
import { Preview } from '../components/Preview'
import { VoiceNote } from '../components/VoiceNote'

interface FeedEvt {
  type: string
  key: string
  size?: number
  top_prefix?: string
  last_modified?: string
  seen_utc?: string
  payload?: any
  wallets?: { address: string; network?: string; standard?: string; confidence?: string; known_operator?: boolean }[]
  amount_inr?: number | null
  kind?: string | null
}

function seedFeed(items: any[] | undefined): FeedEvt[] {
  return (items ?? []).map((r) => ({
    type: 'new_object',
    key: r.key,
    size: r.size,
    last_modified: r.last_modified,
    seen_utc: r.seen_utc,
    payload: r.extract ?? null,
    top_prefix: r.key.includes('/') ? r.key.split('/')[0] + '/' : '',
  }))
}

export function LiveFeed() {
  // Paint the last known arrivals immediately on reload/bucket switch; the
  // effect below revalidates and merges the fresh 24h seed.
  const [events, setEvents] = useState<FeedEvt[]>(() =>
    seedFeed(readApiCache<{ items: any[] }>('/api/recent', { hours: 24, limit: 60 })?.v?.items),
  )
  const [paused, setPaused] = useState(false)
  const [sound, setSound] = useState(true)
  const [filter, setFilter] = useState('')
  const [walletsOnly, setWalletsOnly] = useState(false)
  // Seeded default (review 2026-09-10): the feed mounts with the newest
  // arrivals first (bounded recent history) instead of an empty pane, and
  // live SSE events prepend on top. The live-only toggle narrows to
  // post-mount arrivals when wanted.
  const [liveOnly, setLiveOnly] = useState(false)
  const mountedAt = useRef('')
  const [connected, setConnected] = useState(false)
  const [previewKey, setPreviewKey] = useState<string | null>(null)
  const pausedRef = useRef(paused)
  const soundRef = useRef(sound)
  pausedRef.current = paused
  soundRef.current = sound

  useEffect(() => {
    mountedAt.current = new Date().toISOString().slice(0, 19) + 'Z'
    // Seed with newest arrivals first so the pane is never initially empty.
    const recentParams = { hours: 24, limit: 60 }
    api<{ items: any[] }>('/api/recent', recentParams).then((d) => {
      writeApiCache('/api/recent', recentParams, d)
      const seed = seedFeed(d.items)
      if (seed.length) setEvents((prev) => {
        const known = new Set(prev.map((e) => e.key))
        return [...prev, ...seed.filter((e) => !known.has(e.key))].slice(0, 300)
      })
    }).catch(() => {})
    let retryMs = 1000
    let timer: ReturnType<typeof setTimeout> | null = null
    let es: EventSource | null = null

    function connect() {
      es = new EventSource(eventsUrl())
      es.onopen = () => { setConnected(true); retryMs = 1000 }
      es.onerror = () => {
        setConnected(false); es?.close()
        timer = setTimeout(() => { retryMs = Math.min(retryMs * 2, 30000); connect() }, retryMs)
      }
      es.onmessage = (m) => {
        if (pausedRef.current || !m.data) return
        try {
          const evt: FeedEvt = JSON.parse(m.data)
          setEvents((prev) => [evt, ...prev].slice(0, 300))
          if (soundRef.current && (evt.type === 'new_object' || evt.type === 'wallet_found')) beep(evt.type === 'wallet_found')
        } catch {}
      }
    }
    connect()
    return () => { if (timer) clearTimeout(timer); es?.close() }
  }, [])

  const filtered = (filter ? events.filter((e) => (e.top_prefix ?? '').startsWith(filter)) : events).filter((e) =>
    walletsOnly ? e.type === 'wallet_found' || e.payload?.wallets || e.payload?.vpa : true
  ).filter((e) => !liveOnly || (e.seen_utc ?? '') >= mountedAt.current)
  const walletHits = events.filter((e) => e.type === 'wallet_found').length
  const previewObj = previewKey ? rowFromEvt(events.find((e) => e.key === previewKey)) : null

  // summary stats (real-time deposit/withdrawal)
  const getPrefix = (e: FeedEvt) => (e.top_prefix ?? (e.key.includes('/') ? e.key.split('/')[0] + '/' : ''))
  const deposits = events.filter((e) => getPrefix(e) === 'deposit/')
  const withdrawals = events.filter((e) => getPrefix(e) === 'withdrawal/')
  const otherEvents = events.length - deposits.length - withdrawals.length
  const sumAmount = (arr: FeedEvt[]) => arr.reduce((s, e) => s + (e.amount_inr ?? e.payload?.amount_inr ?? 0), 0)
  const depositAmt = sumAmount(deposits)
  const withdrawalAmt = sumAmount(withdrawals)
  const totalAmt = sumAmount(events)

  return (
    <>
      {/* Header */}
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">⚡</span>
          <div>
            <h1 className="pg-header-title">Live Feed</h1>
            <div className="pg-header-sub">Real-time uploads from the bucket poller</div>
          </div>
        </div>
        <div className="pg-header-right">
          <span className="lf-status ok">{filtered.length} shown</span>
        </div>
      </div>

      {/* Controls */}
      <div className="lf-controls">
        <div className="lf-controls-left">
          <button className="btn" onClick={() => setPaused(!paused)}>
            {paused ? '▶ Resume' : '⏸ Pause'}
          </button>
          <button className="btn" onClick={() => setSound(!sound)}>
            {sound ? '🔔' : '🔕'}
          </button>
          <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ fontSize: 12, padding: '5px 8px' }}>
            <option value="">All prefixes</option>
            <optgroup label="Money Flows">
              <option value="deposit/">deposit/ — Deposits</option>
              <option value="withdrawal/">withdrawal/ — Withdrawals</option>
            </optgroup>
            <optgroup label="Payment QRs">
              <option value="bankingqr/">bankingqr/</option>
              <option value="qrcode/">qrcode/</option>
              <option value="payment/">payment/</option>
            </optgroup>
            <optgroup label="Communications">
              <option value="chat_file/">chat_file/</option>
            </optgroup>
            <optgroup label="Data">
              <option value="csv-files/">csv-files/</option>
              <option value="uploads/">uploads/</option>
            </optgroup>
          </select>
          <button
            className={`ex-toggle ${walletsOnly ? 'ex-toggle-on' : ''}`}
            onClick={() => setWalletsOnly(!walletsOnly)}
            style={{ fontSize: 12 }}
            title="Show only UPI / crypto-wallet payment hits"
          >
            ₿ Wallets only · {walletHits}
          </button>
          <button
            className={`ex-toggle ${liveOnly ? 'ex-toggle-on' : ''}`}
            onClick={() => setLiveOnly(!liveOnly)}
            style={{ fontSize: 12 }}
            title="Live only: show objects that arrived since you opened this page. Turn off to browse the buffered backlog."
          >
            {liveOnly ? '🔴 Live only' : '📦 All'}
          </button>
        </div>
        <div className="lf-controls-right">
          <span className={'lf-status ' + (connected ? (paused ? 'paused' : 'ok') : 'err')}>
            <span className={'dot ' + (connected ? (paused ? 'idle' : 'live') : 'idle')} />
            {connected ? (paused ? 'PAUSED' : 'STREAMING') : 'OFFLINE'}
          </span>
          <button className="btn" onClick={() => setEvents([])} style={{ fontSize: 11, padding: '4px 10px' }} title="Clear the feed buffer (does not affect stored evidence)">Clear feed</button>
        </div>
      </div>

      {/* Real-time deposit/withdrawal summary */}
      <div className="lf-summary-bar">
        <span className="lf-stat deposit">↗ {deposits.length} deposits{depositAmt ? ` · ₹${depositAmt.toLocaleString('en-IN')}` : ''}</span>
        <span className="lf-sep">·</span>
        <span className="lf-stat withdrawal">↙ {withdrawals.length} withdrawals{withdrawalAmt ? ` · ₹${withdrawalAmt.toLocaleString('en-IN')}` : ''}</span>
        <span className="lf-sep">·</span>
        <span className="lf-stat">{otherEvents} other</span>
        <span className="lf-sep">·</span>
        <span className="lf-stat money">₹{totalAmt.toLocaleString('en-IN')} total</span>
        <span className="lf-sep">·</span>
        <span className={`lf-stat ${walletHits ? 'warn' : ''}`}>₿ {walletHits} wallet hits</span>
        <span style={{ flex: 1 }} />
        <span className="lf-stat dim" title="Totals cover the recent buffer only (max 300 events), not the full bucket">{filtered.length} shown / {events.length} buffered (recent 300)</span>
      </div>

      {/* Grid */}
      <div className="lf-grid">
        {filtered.map((e, i) => (
          <FeedCard key={e.key + i} evt={e} flash={i === 0} onOpen={() => setPreviewKey(e.key)} />
        ))}
        {filtered.length === 0 && (
          <div className="lf-empty">
            <div className="lf-empty-icon">⚡</div>
            Waiting for new uploads… keep this tab open.
          </div>
        )}
      </div>

      {previewObj && <Preview obj={previewObj} onClose={() => setPreviewKey(null)} />}
    </>
  )
}

function rowFromEvt(e?: FeedEvt) {
  if (!e) return null as any
  return { key: e.key, size: e.size ?? null, last_modified: e.last_modified ?? e.seen_utc ?? '', extract: e.payload ?? null }
}

function prefixBadge(prefix: string) {
  if (prefix === 'deposit/') return { label: '↗ DEPOSIT', cls: 'ex-badge-money', icon: '💰' }
  if (prefix === 'withdrawal/') return { label: '↙ WITHDRAWAL', cls: 'ex-badge-warn', icon: '💸', style: { borderColor: 'var(--bad)', color: 'var(--bad)' } as any }
  if (prefix === 'bankingqr/') return { label: 'BANKING QR', cls: '', icon: '🏦', style: { borderColor: 'var(--accent)', color: 'var(--accent)' } as any }
  if (prefix === 'qrcode/') return { label: 'QR', cls: '', icon: '🔳' }
  if (prefix === 'payment/') return { label: 'PAYMENT', cls: '', icon: '💳' }
  if (prefix === 'chat_file/') return { label: 'CHAT', cls: '', icon: '💬', style: { borderColor: '#a78bfa', color: '#a78bfa' } as any }
  return null
}

function FeedCard({ evt, flash, onOpen }: { evt: FeedEvt; flash: boolean; onOpen: () => void }) {
  // Preview resilience (mirror-lag, 2026-09-10): thumbnails/full images can 404
  // while an object is still landing in the GCS mirror. Cascade thumb -> full
  // -> timed auto-retry instead of a dead "preview unavailable" box.
  const [imgStage, setImgStage] = useState<'thumb' | 'full' | 'wait' | 'err'>('thumb')
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (retryTimer.current) clearTimeout(retryTimer.current) }, [])
  const onImgErr = () => {
    if (imgStage === 'thumb') setImgStage('full')
    else if (imgStage === 'full' && !retryTimer.current) {
      setImgStage('wait')
      retryTimer.current = setTimeout(() => { retryTimer.current = null; setImgStage('thumb') }, 20000)
    } else if (imgStage === 'wait') setImgStage('err')
  }
  const retryPreview = () => {
    if (retryTimer.current) { clearTimeout(retryTimer.current); retryTimer.current = null }
    setImgStage('thumb')
  }
  const kind = kindOf(evt.key)
  const icon = fileIcon(evt.key)
  const color = fileColor(evt.key)
  const name = evt.key.split('/').pop() || evt.key
  const prefix = (evt.top_prefix ?? (evt.key.split('/')[0] + '/'))
  const badge = prefixBadge(prefix)
  const amt = evt.amount_inr ?? evt.payload?.amount_inr
  const rail = evt.payload?.rail_type ?? evt.payload?.rail ?? ''
  const walletsTop = evt.wallets ?? evt.payload?.wallets ?? []

  return (
    <div className={`lf-card ${flash ? 'flash' : ''}`}>
      {kind === 'image' && (imgStage === 'thumb' || imgStage === 'full') && (
        <img
          className="lf-thumb"
          src={imgStage === 'thumb' ? objectUrl(evt.key, false, 320) : objectUrl(evt.key)}
          loading="lazy" onClick={onOpen} alt="" onError={onImgErr}
        />
      )}
      {kind === 'image' && imgStage === 'wait' && (
        <div style={{ padding: '20px 0', textAlign: 'center', background: `${color}08` }} className="tiny dim" onClick={onOpen} title="Object still landing in the mirror — retrying">
          ⏳ landing in mirror — retrying…
        </div>
      )}
      {kind === 'image' && imgStage === 'err' && (
        <div style={{ padding: '20px 0', textAlign: 'center', background: `${color}08` }} className="tiny dim" onClick={retryPreview} title="Click to retry">
          🖼️ preview unavailable — click to retry
        </div>
      )}
      {kind === 'audio' && (
        <div className="lf-audio-wrap">
          <VoiceNote objKey={evt.key} size={evt.size} compact />
        </div>
      )}
      {kind !== 'image' && kind !== 'audio' && (
        <div style={{ padding: '20px 0', textAlign: 'center', background: `${color}08` }}>
          <span style={{ fontSize: 28, color }}>{icon}</span>
        </div>
      )}
      <div className="lf-body">
        {/* Prefix badge row */}
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 6 }}>
          {badge && <span className={`ex-badge ${badge.cls}`} style={(badge as any).style}>{badge.label}</span>}
          {evt.type === 'wallet_found' && (
            <span className="ex-badge ex-badge-new" style={{ fontWeight: 700 }}>₿ WALLET HIT</span>
          )}
          {evt.type !== 'new_object' && evt.type !== 'wallet_found' && <span className="ex-badge">{evt.type}</span>}
        </div>
        {/* Prominent amount for money events */}
        {amt != null && (
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 16, fontWeight: 800, color: prefix === 'withdrawal/' ? 'var(--bad)' : 'var(--money)' }}>
              {prefix === 'withdrawal/' ? '−' : prefix === 'deposit/' ? '+' : ''}₹{amt.toLocaleString('en-IN')}
            </span>
            {rail && <span className="ex-badge" title={rail} style={{ fontSize: 10 }}>{rail}</span>}
          </div>
        )}
        {walletsTop?.length > 0 && walletsTop.map((w: any) => (
          <div key={w.address} className="mono" style={{ fontSize: 11, margin: '2px 0', fontWeight: w.known_operator ? 400 : 700 }}>
            <span className="ex-badge" style={{ marginRight: 6, borderColor: w.known_operator ? 'var(--accent2)' : undefined, color: w.known_operator ? 'var(--accent2)' : undefined }}>
              {w.network ?? '?'}{w.known_operator ? ' · known' : ' · NEW'}
            </span>
            {String(w.address).slice(0, 28)}
            <span style={{ marginLeft: 8 }}>
              <a href={arkhamAddressUrl(String(w.address))} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} style={{ fontSize: 10 }}>Arkham ↗</a>
              {' · '}
              <a href={bestTronscanUrl(String(w.address), (w as any).txids) ?? '#'} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} style={{ fontSize: 10 }}>{tronscanLabel((w as any).txids)}</a>
              {' · '}
              <a href={traceDeskAddressUrl(String(w.address))} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} style={{ fontSize: 10 }}>TraceDesk ↗</a>
            </span>
          </div>
        ))}
        {evt.payload?.vpa && (
          <span className={`ex-badge ${evt.payload.new_mule ? 'ex-badge-new' : ''}`} style={{ marginBottom: 4 }}>
            {evt.payload.vpa}{evt.payload.new_mule ? ' · NEW MULE' : ''}
          </span>
        )}
        {evt.payload?.vpas && Array.isArray(evt.payload.vpas) && evt.payload.vpas.length > 1 && (
          <span className="ex-badge" style={{ marginLeft: 4 }}>+{evt.payload.vpas.length - 1} more</span>
        )}
        <div className="lf-key mono">{name}</div>
        <div className="mono tiny dim" style={{ marginTop: 2, opacity: .6 }}>{prefix}</div>
        <div className="lf-meta">
          <span>{fmtBytes(evt.size)}</span>
          <span title={evt.last_modified && evt.seen_utc ? `Uploaded (bucket): ${fmtBoth(evt.last_modified)} · In mirror since: ${fmtBoth(evt.seen_utc)}` : ''}>
            {fmtIST(evt.last_modified || evt.seen_utc)}
            {evt.last_modified && evt.seen_utc && evt.seen_utc.slice(0, 10) !== evt.last_modified.slice(0, 10) && (
              <> · ⏫ {fmtIST(evt.seen_utc)}</>
            )}
          </span>
          <a onClick={onOpen}>inspect</a>
          <a href={objectUrl(evt.key, true)} download>⛨ get</a>
        </div>
      </div>
    </div>
  )
}

let audioCtx: AudioContext | null = null
function beep(wallet = false) {
  try {
    audioCtx = audioCtx || new AudioContext()
    const o = audioCtx.createOscillator()
    const g = audioCtx.createGain()
    o.connect(g); g.connect(audioCtx.destination)
    o.frequency.value = wallet ? 1320 : 880
    g.gain.setValueAtTime(0.06, audioCtx.currentTime)
    o.start(); o.stop(audioCtx.currentTime + (wallet ? 0.25 : 0.12))
  } catch {}
}
