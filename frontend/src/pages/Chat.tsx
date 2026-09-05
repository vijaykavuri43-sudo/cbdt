import { useCallback, useEffect, useRef, useState } from 'react'
import { api, eventsUrl, objectUrl, post, type ObjRow } from '../lib/api'
import { extOf, fmtBytes, fmtTime } from '../lib/format'
import { Preview } from '../components/Preview'

function parseParticipants(key: string): { from: string; to: string | null; fileLabel: string } {
  const base = key.split('/').pop() || ''
  // chat_file/<from>_<to>_<ts>_<rest>  or  chat_file/<from>_<ts>_<rest>
  // timestamp is 10-13 digit starting 15xxxxx... or 16/17/18..., detect numeric token
  const parts = base.split('_')
  const isTs = (s: string) => /^\d{10,13}$/.test(s)
  if (parts.length >= 3 && !isTs(parts[1]) && isTs(parts[2])) {
    const from = parts[0] || 'unknown'
    const to = parts[1]
    const fileLabel = parts.slice(3).join('_') || parts.slice(2).join('_')
    return { from, to, fileLabel }
  }
  if (parts.length >= 2 && isTs(parts[1])) {
    return { from: parts[0] || 'unknown', to: null, fileLabel: parts.slice(2).join('_') || base }
  }
  // fallback
  const p = parts[0] || 'unknown'
  return { from: p, to: null, fileLabel: base }
}
function _senderOf(key: string): string { return parseParticipants(key).from }
void _senderOf

function timeAgo(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime()
  if (diff < 60000) return 'now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  return `${Math.floor(diff / 86400000)}d ago`
}

function dayKey(ts: string): string { return ts.slice(0, 10) }

function formatDay(ts: string): string {
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const dd = new Date(ts.slice(0, 10)); dd.setHours(0, 0, 0, 0)
  const diff = Math.round((today.getTime() - dd.getTime()) / 86400000)
  if (diff === 0) return 'Today'
  if (diff === 1) return 'Yesterday'
  return new Date(ts).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })
}

export function Chat() {
  const [filter, setFilter] = useState<'all' | 'voice' | 'images'>('all')
  const [q, setQ] = useState('')
  const [items, setItems] = useState<ObjRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [preview, setPreview] = useState<ObjRow | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [liveCount, setLiveCount] = useState(0)
  // Keys whose <audio> element fired onError (codec the browser can't decode,
  // e.g. AMR, or a stale-cache wrong content-type) -> render download fallback.
  const [audioErr, setAudioErr] = useState<Record<string, boolean>>({})
  const limit = 50
  const listRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    let ext = ''
    if (filter === 'voice') ext = '.wav,.mp3,.ogg,.amr'
    if (filter === 'images') ext = '.jpg,.jpeg,.png,.webp'
    api<any>('/api/list', { prefix: 'chat_file/', order: '-modified', limit, offset: page * limit, ext, q })
      .then((d) => { setItems(d.items ?? []); setTotal(d.total ?? 0) })
      .catch(() => {})
  }, [filter, q, page])

  useEffect(load, [load])
  useEffect(() => setPage(0), [filter, q])

  useEffect(() => {
    let retryMs = 1000
    let timer: ReturnType<typeof setTimeout> | null = null
    let es: EventSource | null = null
    function connect() {
      es = new EventSource(eventsUrl())
      es.onopen = () => { retryMs = 1000 }
      es.onerror = () => { es?.close(); timer = setTimeout(() => { retryMs = Math.min(retryMs * 2, 30000); connect() }, retryMs) }
      es.onmessage = (m) => { try { const e = JSON.parse(m.data); if (e.top_prefix === 'chat_file/' || e.key?.startsWith('chat_file/')) setLiveCount((c) => c + 1) } catch {} }
    }
    connect()
    return () => { if (timer) clearTimeout(timer); es?.close() }
  }, [])

  async function doSync() {
    setSyncing(true)
    try { await post<any>('/api/sync?prefix=chat_file/'); load(); setLiveCount(0) }
    finally { setSyncing(false) }
  }

  const pages = Math.ceil(total / limit)

  return (
    <>
      {/* Header */}
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">💬</span>
          <div>
            <h1 className="pg-header-title">Live Chats</h1>
            <div className="pg-header-sub">chat_file/ · {total.toLocaleString()} messages · newest first</div>
          </div>
        </div>
        <div className="pg-header-right">
          {liveCount > 0 && <span className="ex-badge ex-badge-new" style={{ animation: 'pulse 1s infinite' }}>+{liveCount} live</span>}
          <button className="btn btn-primary" onClick={doSync} disabled={syncing} style={{ fontSize: 12 }}>
            {syncing ? 'Syncing…' : '↻ Sync'}
          </button>
        </div>
      </div>

      {/* Toolbar */}
      <div className="lf-controls">
        <div className="lf-controls-left">
          <div className="ex-search-wrap">
            <span className="ex-search-icon">⌕</span>
            <input type="text" placeholder="Search sender…" value={q} onChange={(e) => setQ(e.target.value)} className="ex-search" style={{ width: 200 }} />
          </div>
          <button className={`ex-toggle ${filter === 'all' ? 'ex-toggle-on' : ''}`} onClick={() => setFilter('all')}>All · {filter === 'all' ? total.toLocaleString() : '—'}</button>
          <button className={`ex-toggle ${filter === 'images' ? 'ex-toggle-on' : ''}`} onClick={() => setFilter('images')}>🖼 Images</button>
          <button className={`ex-toggle ${filter === 'voice' ? 'ex-toggle-on' : ''}`} onClick={() => setFilter('voice')}>🎙 Voice</button>
        </div>
        <div className="lf-controls-right">
          <span className="small dim">{liveCount > 0 ? `+${liveCount} live` : `${total.toLocaleString()} total`}</span>
        </div>
      </div>

      {/* Messages */}
      <div className="ch-list" ref={listRef}>
        {items.map((o, idx) => {
          const isVoice = ['.wav', '.mp3', '.ogg', '.amr'].includes(extOf(o.key))
          const isImg = ['.jpg', '.jpeg', '.png', '.webp', '.gif'].includes(extOf(o.key))
          const { from, to, fileLabel } = parseParticipants(o.key)
          const shortName = fileLabel || o.key.split('/').pop() || o.key
          const convKey = to ? `${from}→${to}` : from
          const prev = idx > 0 ? items[idx - 1] : null
          const prevConv = prev ? (()=>{ const p=parseParticipants(prev.key); return p.to?`${p.from}→${p.to}`:p.from})() : null
          const showDay = !prev || dayKey(prev.last_modified) !== dayKey(o.last_modified)
          const sameSender = prevConv === convKey && !showDay
          const isWay = to != null
          return (
            <div key={o.key}>
              {showDay && (
                <div className="ch-day-sep">
                  <span className="ch-day-badge">{formatDay(o.last_modified)}</span>
                </div>
              )}
              <div className={`ch-msg ${sameSender ? 'grouped' : ''} ${isWay ? 'ch-msg-way' : ''}`}>
                {!sameSender ? (
                  <div className="ch-avatar" title={convKey}>{from.slice(0, 2).toUpperCase()}</div>
                ) : (
                  <div className="ch-avatar-placeholder ch-time-gutter" title={fmtTime(o.last_modified)}>{fmtTime(o.last_modified).slice(11, 16)}</div>
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  {!sameSender && (
                    <>
                      <div className="ch-meta">
                        <span className="ch-sender">{from}</span>
                        {to && <><span className="ch-way">→</span><span className="ch-sender to">{to}</span></>}
                        <span className="ch-time">{fmtTime(o.last_modified).slice(11, 16)}</span>
                        <span className="tiny dim">· {timeAgo(o.last_modified)} · {fmtBytes(o.size)}</span>
                        {o.extract?.amount_inr != null && <span className="ex-badge ex-badge-money">₹{o.extract.amount_inr.toLocaleString('en-IN')}</span>}
                        {o.extract?.vpa && <span className="ex-badge" title={o.extract.vpa}>{o.extract.vpa.slice(0, 24)}</span>}
                      </div>
                      <div className="ch-filename" title={o.key}>{shortName}</div>
                    </>
                  )}
                  <div style={{ marginTop: sameSender ? 0 : 6 }}>
                    {isImg ? (
                      <img className="ch-img" src={objectUrl(o.key, false, 320)} alt="" loading="lazy" onClick={() => setPreview(o)} />
                    ) : isVoice ? (
                      audioErr[o.key] ? (
                        <div className="ch-audio-wrap ch-audio-err">
                          <span style={{ fontSize: 12 }}>⚠</span>
                          <span className="tiny dim">browser can't preview this clip</span>
                          <a className="btn" style={{ padding: '2px 8px', fontSize: 11, height: 24 }} href={objectUrl(o.key, true)} download title="Download with evidence hash">⛨ Download</a>
                        </div>
                      ) : (
                        <div className="ch-audio-wrap">
                          <span style={{ fontSize: 14 }}>🎙️</span>
                          <audio controls src={objectUrl(o.key)} className="ch-audio" preload="metadata"
                            onError={() => setAudioErr((m) => (m[o.key] ? m : { ...m, [o.key]: true }))} />
                          <span className="tiny dim mono">{fmtBytes(o.size)}</span>
                        </div>
                      )
                    ) : (
                      <a onClick={() => setPreview(o)} style={{ color: 'var(--accent)', cursor: 'pointer', fontSize: 12 }}>
                        Open file ({fmtBytes(o.size)})
                      </a>
                    )}
                  </div>
                </div>
                <a className="btn" style={{ padding: '4px 8px', fontSize: 11, height: 26, alignSelf: 'flex-start' }} href={objectUrl(o.key, true)} download title="Download with evidence hash">
                  ⛨
                </a>
              </div>
            </div>
          )
        })}
        {items.length === 0 && <div className="dp-empty"><div className="dp-empty-icon">💬</div>No messages in this view.</div>}
      </div>

      {/* Pagination */}
      <div className="ex-pagination">
        <button className="ex-page-btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Newer</button>
        <span className="ex-page-info">page {page + 1}/{pages || 1} · {total.toLocaleString()} total</span>
        <button className="ex-page-btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Older →</button>
      </div>

      {preview && <Preview obj={preview} onClose={() => setPreview(null)} />}
    </>
  )
}
