import { useCallback, useEffect, useState } from 'react'
import { api, eventsUrl, AuthError, tabHidden, type Status } from '../lib/api'
import { fmtBytes, fmtINR, fmtTime } from '../lib/format'
import { OfficerStrip } from '../components/OfficerStrip'
import { folderInfo } from '../lib/format'

interface Stats {
  objects_total: number
  per_prefix: { p: string; n: number; s: number; newest: string }[]
  day_histogram: { day: string; objects: number; bytes: number }[]
  deposits: { count: number; sum_amount_inr: number }
  withdrawals: { count: number; sum_amount_inr: number }
}

interface Types {
  categories: { id: string; label: string; count: number; bytes: number }[]
  extensions: [string, number][]
}

interface BucketHealth {
  live: boolean
  auth?: boolean
  latency_ms?: number
  checked_utc?: string
  error?: string
  effective_source?: string
}

interface MirrorSync {
  daemon_seen: boolean
  healthy?: boolean
  status_age_min?: number | null
  verdict: string
  error?: string
  status?: {
    utc?: string
    cycle?: number
    cycle_took_sec?: number
    scanned?: number
    scanned_this_cycle?: number
    scanned_total?: number
    queued?: number
    queued_pending?: number
    uploaded_total?: number
    errors?: number
    oversized_skipped?: number
    gcs_mirror_objects?: number
    source_inventory_objects?: number
    source_inventory_bytes?: number
    source_inventory_prefixes_scanned?: number
    source_inventory_prefixes_total?: number
    source_inventory_complete?: boolean
    source_inventory_oldest_scan_utc?: string | null
    preserved_only_estimate?: number | null
    tombstones_pending?: number
    tombstones_terminal?: number
    dashboard_notified?: number
    dashboard_notify_errors?: number
    s3_bucket?: string
    gcs_target?: string
  }
}

interface NetUrlRow {
  host: string; url: string; source: string; hits: number; txns: number;
  amount_inr: number; customers: number; utrs: number;
  first_seen: string; last_seen: string; sample_key: string;
}

export function StatusPage() {
  const [st, setSt] = useState<Status | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [types, setTypes] = useState<Types | null>(null)
  const [health, setHealth] = useState<BucketHealth | null>(null)
  const [mirrorSync, setMirrorSync] = useState<MirrorSync | null>(null)
  const [netUrls, setNetUrls] = useState<{ rows: NetUrlRow[]; counts: any } | null>(null)
  const [pinging, setPinging] = useState(false)
  const [liveConnected, setLiveConnected] = useState(false)
  const [lastEventAt, setLastEventAt] = useState<string | null>(null)
  const [eventPulse, setEventPulse] = useState(false)
  const doRefresh = useCallback(() => {
    api<Stats>('/api/stats').then(setStats).catch(() => {})
    api<Status>('/api/status').then(setSt).catch(() => {})
    api<{ rows: NetUrlRow[]; counts: any }>('/api/network-urls').then(setNetUrls).catch(() => {})
  }, [])
  const refreshStats = doRefresh

  useEffect(() => {
    let alive = true
    const load = () =>
      Promise.all([api<Status>('/api/status'), api<Stats>('/api/stats')])
        .then(([s, x]) => { if (alive) { setSt(s); setStats(x) } })
        .catch(() => {})
    load()
    const t = setInterval(() => { if (!tabHidden()) load() }, st?.sweep_done ? 30000 : 8000)
    return () => { alive = false; clearInterval(t) }
  }, [st?.sweep_done])

  useEffect(() => {
    let es: EventSource | null = null
    let retryMs = 1000
    let timer: ReturnType<typeof setTimeout> | null = null
    function connect() {
      es = new EventSource(eventsUrl())
      es.onopen = () => { setLiveConnected(true); retryMs = 1000 }
      es.onerror = () => {
        setLiveConnected(false); es?.close()
        timer = setTimeout(() => { retryMs = Math.min(retryMs * 2, 30000); connect() }, retryMs)
      }
      es.onmessage = (m) => {
        if (!m.data) return
        try {
          const evt = JSON.parse(m.data)
          setLastEventAt(new Date().toISOString())
          setEventPulse(true); setTimeout(() => setEventPulse(false), 800)
          if (evt.type === 'enriched' || evt.type === 'new_object' || evt.type === 'wallet_found') {
            refreshStats()
          }
        } catch {}
      }
    }
    connect()
    return () => { if (timer) clearTimeout(timer); es?.close(); setLiveConnected(false) }
  }, [refreshStats])

  useEffect(() => {
    let alive = true
    const loadTypes = () => api<Types>('/api/types').then((t) => alive && setTypes(t)).catch(() => {})
    loadTypes()
    const t = setInterval(loadTypes, 300000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const ping = useCallback(() => {
    setPinging(true)
    api<BucketHealth>('/api/bucket-health').then(setHealth).catch((e) => {
      setHealth(e instanceof AuthError ? { live: false, auth: true } : { live: false })
    }).finally(() => setPinging(false))
    api<MirrorSync>('/api/mirror-sync').then(setMirrorSync).catch(() => setMirrorSync({ daemon_seen: false, verdict: 'UNKNOWN - endpoint unavailable' }))
  }, [])
  useEffect(ping, [ping])

  const sweepVals = Object.values(st?.sweep_progress ?? {})
  const swept = sweepVals.reduce((a, v) => a + (v.count ?? 0), 0)
  const sweepTarget = st?.objects_total && st.objects_total > swept ? st.objects_total : swept + 10000
  const sweepPct = Math.min(99, Math.floor((swept / Math.max(1, sweepTarget)) * 100))
  const totalBytes = stats?.per_prefix?.reduce((a, r) => a + (r.s ?? 0), 0) ?? 0
  const maxPrefixCount = Math.max(...(stats?.per_prefix ?? []).map((r) => r.n), 1)

  function exportNetCsv() {
    const q = (v: any) => '"' + String(v ?? '').replace(/"/g, '""') + '"'
    const header = 'Host,Source,Hits,Txns,Amount_INR,Customers,UTRs,First_seen,Last_seen,Sample_URL,Evidence_key\n'
    const rows = (netUrls?.rows ?? [])
      .map((r) => [q(r.host), q(r.source), r.hits, r.txns, r.amount_inr, r.customers, r.utrs, q(r.first_seen), q(r.last_seen), q(r.url), q(r.sample_key)].join(','))
      .join('\n')
    const blob = new Blob([header + rows], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `network-urls-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">◉</span>
          <div>
            <h1 className="pg-header-title">Overview</h1>
            <div className="pg-header-sub">Officer briefing · {st?.case ?? 'CASE-2026-CBTF'} · {st?.effective_source ?? 'gcs'} mirror</div>
          </div>
        </div>
        <div className="pg-header-right">
          <span className={'lf-status ' + (liveConnected ? 'ok' : 'err')} style={{ fontSize: 11 }}>
            <span className={'dot ' + (liveConnected ? 'live' : 'idle')} />
            {liveConnected ? 'LIVE' : 'RECONNECTING'}
          </span>
          {lastEventAt && <span className="tiny dim" style={{ marginLeft: 8 }}>{new Date(lastEventAt).toLocaleTimeString()} last event</span>}
        </div>
      </div>

      <OfficerStrip>
        <div className={`st-card ${st?.phase === 'live-polling' ? 'st-card-accent' : ''}`}>
          <div className="st-card-icon">📡</div>
          <div className="st-card-label">Collector</div>
          <div className="st-card-value accent" style={{ fontSize: 16, fontWeight: 700 }}>
            <span className="phase" style={{ fontSize: 13 }}>
              <span className={'dot ' + (st?.phase === 'live-polling' ? 'live' : st?.phase === 'sweep' ? 'sweep' : 'idle')} />
              {(st?.phase ?? 'connecting').toUpperCase()}
            </span>
          </div>
        </div>

        <div className="st-card st-card-accent">
          <div className="st-card-icon">📦</div>
          <div className="st-card-label">Preserved objects</div>
          <div className="st-card-value accent">{(st?.objects_total ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">{fmtBytes(totalBytes)} total data</div>
        </div>

        <div className={`st-card st-card-money ${liveConnected && eventPulse ? 'st-card-live' : ''}`}>
          <div className="st-card-icon">💰 {liveConnected && <span className="dot live" style={{ display: 'inline-block', verticalAlign: 'middle', marginLeft: 6 }} />}</div>
          <div className="st-card-label">Deposits (all parsed)</div>
          <div className="st-card-value money">{fmtINR(stats?.deposits.sum_amount_inr)}</div>
          <div className="st-card-sub">{(stats?.deposits.count ?? 0).toLocaleString()} slips · OCR/QR</div>
        </div>

        <div className={`st-card ${liveConnected && eventPulse ? 'st-card-live' : ''}`}>
          <div className="st-card-icon">💸 {liveConnected && <span className="dot live" style={{ display: 'inline-block', verticalAlign: 'middle', marginLeft: 6 }} />}</div>
          <div className="st-card-label">Withdrawals (all parsed)</div>
          <div className="st-card-value">{fmtINR(stats?.withdrawals.sum_amount_inr)}</div>
          <div className="st-card-sub">{(stats?.withdrawals.count ?? 0).toLocaleString()} slips</div>
        </div>

        <div className="st-card st-card-accent">
          <div className="st-card-icon">🔍</div>
          <div className="st-card-label">OCR / QR decoded</div>
          <div className="st-card-value accent">{st?.enrichment?.with_payload?.toLocaleString() ?? '—'}</div>
          <div className="st-card-sub">{st?.enrichment?.coverage_pct != null ? `${st.enrichment.coverage_pct}% coverage` : '—'} · {(st?.enrichment?.slip_backlog ?? 0).toLocaleString()} older slips queued</div>
          <div className="tiny dim" style={{ marginTop: 4 }}>{st?.gemini_enabled ? `Gemini ${st?.gemini_model}` : 'Tesseract only'}</div>
        </div>
      </OfficerStrip>

      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Pipeline</span>
          <button className="btn" onClick={ping} disabled={pinging} style={{ fontSize: 11, padding: '4px 10px' }}>
            {pinging ? 'Pinging...' : '↻ Probe'}
          </button>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
          <span className="phase">
            <span className={'dot ' + (health?.live ? 'live' : 'idle')} />
            {health === null ? 'PROBING...' : health.live ? `GCS live · ${health.latency_ms} ms` : ((health as any).auth ? 'AUTH REQUIRED' : 'GCS UNREACHABLE')}
          </span>
          <span className="phase" style={{ color: mirrorSync?.healthy ? 'var(--accent2)' : 'var(--warn)' }}>
            <span className={'dot ' + (mirrorSync?.healthy ? 'live' : 'idle')} />
            {mirrorSync?.healthy ? 'S3→GCS syncing' : (mirrorSync?.daemon_seen ? 'SYNC STALE' : 'NO SYNC DAEMON')}
          </span>
          <span className="small dim">poll {fmtTime(st?.last_poll_utc)} · +{(st?.new_since_start ?? 0).toLocaleString()} this session</span>
          {st?.last_poll_utc && (Date.now() - new Date(st.last_poll_utc).getTime()) / 1000 > 90 && (
            <span style={{ color: 'var(--warn)', fontSize: 12 }}>⚠ stale poll</span>
          )}
        </div>
        {health && !health.live && health.error && <div className="small" style={{ color: 'var(--bad)', marginTop: 6 }}>{health.error}</div>}
        {mirrorSync && (
          <div className="sync-metrics" style={{ marginTop: 10 }}>
            <div><span className="tiny dim">S3 NOW</span><strong>{mirrorSync.status?.source_inventory_objects?.toLocaleString() ?? '...'}</strong></div>
            <div><span className="tiny dim">GCS MIRROR</span><strong>{mirrorSync.status?.gcs_mirror_objects?.toLocaleString() ?? '—'}</strong></div>
            <div><span className="tiny dim">PENDING COPY</span><strong>{(mirrorSync.status?.queued_pending ?? 0).toLocaleString()}</strong></div>
            <div><span className="tiny dim">EXCEPTIONS</span><strong>{((mirrorSync.status?.tombstones_pending ?? 0) + (mirrorSync.status?.tombstones_terminal ?? 0)).toLocaleString()}</strong></div>
          </div>
        )}
        {mirrorSync?.status?.errors ? <div className="tiny" style={{ color: 'var(--bad)', marginTop: 6 }}>sync errors {mirrorSync.status.errors}</div> : null}
      </div>

      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">What's in the bucket</span>
        </div>
        {types ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {types.categories.filter((c) => c.count > 0).map((c) => {
              const maxBytes = Math.max(...types.categories.map((x) => x.bytes), 1)
              return (
                <div key={c.id} className="st-prefix-row">
                  <span className="st-prefix-count">{c.count.toLocaleString()}</span>
                  <span style={{ flex: 1, fontSize: 12.5 }}>{c.label}</span>
                  <div className="st-prefix-bar">
                    <div className="st-prefix-bar-fill" style={{ width: `${(c.bytes / maxBytes) * 100}%` }} />
                  </div>
                  <span className="st-prefix-size">{fmtBytes(c.bytes)}</span>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="small dim">Census loading...</div>
        )}
      </div>

      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Uploads - last 14 days (UTC)</span>
        </div>
        {stats && stats.day_histogram.some((d) => d.objects > 0) ? (
          <>
            <Histogram data={stats.day_histogram} />
            <div className="small dim" style={{ marginTop: 10, lineHeight: 1.5, clear: 'both' }}>
              By source last-modified (bucket time), not when we indexed it.
              {(mirrorSync?.status?.queued_pending ?? 0) > 0
                ? ` Mirror backlog ${(mirrorSync?.status?.queued_pending ?? 0).toLocaleString()} pending.`
                : ' Mirror caught up.'}
            </div>
          </>
        ) : (
          <div className="small dim">{st?.sweep_done ? 'No uploads in window.' : 'Waiting for sweep...'}</div>
        )}
      </div>

      {!st?.sweep_done && (
        <div className="st-panel">
          <div className="st-panel-head">
            <span className="st-panel-title">Initial manifest sweep</span>
            <span className="small dim">{sweepPct}%</span>
          </div>
          <div className="st-progress">
            <div className="st-progress-fill" style={{ width: `${sweepPct}%` }} />
          </div>
          <div className="small dim mono">
            {Object.entries(st?.sweep_progress ?? {}).length}/{st?.prefixes.length ?? 28} prefixes · {swept.toLocaleString()} of {(st?.objects_total ?? 0).toLocaleString()} keys
          </div>
        </div>
      )}

      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Folders (exact paths)</span>
          <span className="tiny dim">Click a path to open it in Explorer</span>
        </div>
        {(stats?.per_prefix ?? []).map((r) => {
          const info = folderInfo(r.p)
          const path = r.p || '/'
          return (
            <button
              type="button"
              key={r.p}
              className="st-prefix-row st-prefix-row-btn"
              title={`Open ${path} in Explorer`}
              onClick={() => window.dispatchEvent(new CustomEvent('s3lm-goto', { detail: { tab: 'explorer', prefix: r.p } }))}
            >
              <span className="st-prefix-icon" style={{ color: info.color }}>{info.icon}</span>
              <span className="st-prefix-name mono">{path}</span>
              <div className="st-prefix-bar">
                <div className="st-prefix-bar-fill" style={{ width: `${(r.n / maxPrefixCount) * 100}%` }} />
              </div>
              <span className="st-prefix-count">{r.n.toLocaleString()}</span>
              <span className="st-prefix-size">{fmtBytes(r.s)}</span>
            </button>
          )
        })}
      </div>

      <div className="st-panel" style={{ marginBottom: 0 }}>
        <div className="st-panel-head">
          <span className="st-panel-title">Network hosts ({(netUrls?.counts?.total ?? 0).toLocaleString()})</span>
          <button className="btn" onClick={exportNetCsv} disabled={!netUrls?.rows?.length} style={{ fontSize: 11, padding: '4px 10px' }}>
            CSV ({(netUrls?.rows ?? []).length})
          </button>
        </div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr>
          <th>Host</th><th>Source</th><th className="num">Hits</th><th className="num">Amount</th><th>Sample</th>
        </tr></thead>
          <tbody>{(netUrls?.rows ?? []).slice(0, 40).map((r) => <tr key={r.source + ':' + r.host}>
            <td className="mono" style={{ fontWeight: 700 }}>{r.host}</td>
            <td className="small">{r.source}</td>
            <td className="num">{r.hits.toLocaleString()}</td>
            <td className="num money">{r.source === 'gateway-brand' ? fmtINR(r.amount_inr) : '—'}</td>
            <td className="mono small" style={{ maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.url}>{r.url}</td>
          </tr>)}{!netUrls && <tr><td colSpan={5} className="dim">Loading...</td></tr>}</tbody></table></div>
      </div>
    </>
  )
}

function Histogram({ data }: { data: Stats['day_histogram'] }) {
  const max = Math.max(...data.map((d) => d.objects), 1)
  return (
    <div className="st-hbar">
      {data.map((d) => (
        <div
          key={d.day}
          className="st-bar"
          data-tip={`${d.day}: ${d.objects.toLocaleString()} objs · ${fmtBytes(d.bytes)}`}
          style={{ height: `${Math.max(2, (d.objects / max) * 100)}%` }}
        >
          <span className="st-bar-lbl">{d.day.slice(5)}</span>
        </div>
      ))}
    </div>
  )
}
