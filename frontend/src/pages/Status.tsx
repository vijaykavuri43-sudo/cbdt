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

  // SSE for live deposit/withdrawal badge + stat refresh
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
      // bucket-health is unauthenticated: a network/HTTP failure here is a
      // genuine reachability problem. But an AuthError means the SESSION is
      // stale, not the bucket — report "unknown" instead of the misleading
      // "not reachable" (review 2026-09-02; endpoint no longer returns 401).
      setHealth(e instanceof AuthError ? { live: false, auth: true } : { live: false })
    }).finally(() => setPinging(false))
    api<MirrorSync>('/api/mirror-sync').then(setMirrorSync).catch(() => setMirrorSync({ daemon_seen: false, verdict: 'UNKNOWN — endpoint unavailable' }))
  }, [])
  useEffect(ping, [ping])

  const sweepVals = Object.values(st?.sweep_progress ?? {})
  const swept = sweepVals.reduce((a, v) => a + (v.count ?? 0), 0)
  // Denominator: known object total from a previous index; fall back to a stable
  // growing estimate so the bar never reads 100% while keys are still arriving.
  const sweepTarget = st?.objects_total && st.objects_total > swept ? st.objects_total : swept + 10000
  const sweepPct = Math.min(99, Math.floor((swept / Math.max(1, sweepTarget)) * 100))
  const totalBytes = stats?.per_prefix?.reduce((a, r) => a + (r.s ?? 0), 0) ?? 0
  const maxPrefixCount = Math.max(...(stats?.per_prefix ?? []).map((r) => r.n), 1)

  function exportNetCsv() {
    // Officer download: every network URL row (brands + QR hosts) with volume info.
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
      {/* Header */}
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">◉</span>
          <div>
            <h1 className="pg-header-title">Overview</h1>
            <div className="pg-header-sub">Read-only monitoring · {st?.auth_ref} · Source: {st?.effective_source ?? st?.object_source ?? 'gcs'}{st?.effective_source === 's3-fallback' ? ' (direct S3 — GCS creds absent)' : ''} · gcs_mirror: {st?.bucket_url?.replace(/^gs:\/\//, '') ?? '…'}</div>
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

      {/* Officer briefing — last-24h live money movement + rail triage */}
      <OfficerStrip />

      {/* Stat cards — 12 Sep officer row, without New-since-start */}
      <div className="st-cards ov-index">
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
          <div className="st-card-label">Preserved objects indexed</div>
          <div className="st-card-value accent">{(st?.objects_total ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">{fmtBytes(totalBytes)} total data</div>
        </div>

        <div className={`st-card st-card-money ${liveConnected && eventPulse ? 'st-card-live' : ''}`}>
          <div className="st-card-icon">💰 {liveConnected && <span className="dot live" style={{ display: 'inline-block', verticalAlign: 'middle', marginLeft: 6 }} />}</div>
          <div className="st-card-label">Gambler funds → receiving rails</div>
          <div className="st-card-value money">{fmtINR(stats?.deposits.sum_amount_inr)}</div>
          <div className="st-card-sub">{(stats?.deposits.count ?? 0).toLocaleString()} deposit slips parsed · full case history since 22 Jul 2026 (not the 24h briefing above) · withdrawals tracked separately</div>
          <div className="tiny dim" style={{ marginTop: 4, lineHeight: 1.3 }}>Principal only · promo/balance ignored · ≤{fmtINR(st?.max_amount_inr ?? 10000000)} · persisted enriched=1</div>
        </div>

        <div className={`st-card ${liveConnected && eventPulse ? 'st-card-live' : ''}`}>
          <div className="st-card-icon">💸 {liveConnected && <span className="dot live" style={{ display: 'inline-block', verticalAlign: 'middle', marginLeft: 6 }} />}</div>
          <div className="st-card-label">Withdrawals — LIVE</div>
          <div className="st-card-value">{fmtINR(stats?.withdrawals.sum_amount_inr)}</div>
          <div className="st-card-sub">{(stats?.withdrawals.count ?? 0).toLocaleString()} withdrawal slips</div>
          <div className="tiny dim" style={{ marginTop: 4, lineHeight: 1.3 }}>Principal only · ≤{fmtINR(st?.max_amount_inr ?? 10000000)} · persisted</div>
        </div>

        <div className="st-card st-card-accent">
          <div className="st-card-icon">🔍</div>
          <div className="st-card-label">Enrichment (OCR/QR)</div>
          <div className="st-card-value accent">{st?.enrichment?.with_payload?.toLocaleString() ?? '—'}</div>
          <div className="st-card-sub">{st?.enrichment?.pending_now?.toLocaleString() ?? '—'} live queue · {(st?.enrichment?.slip_backlog ?? 0).toLocaleString()} older slips backfilling</div>
          <div className="tiny dim" style={{ marginTop: 4 }}>{st?.gemini_enabled ? `Gemini ${st?.gemini_model} active` : 'Tesseract only — set GEMINI_API_KEY for verification'}</div>
        </div>
      </div>

      {/* Bucket health */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Live GCS mirror connectivity</span>
          <button className="btn" onClick={ping} disabled={pinging} style={{ fontSize: 11, padding: '4px 10px' }}>
            {pinging ? 'Pinging…' : '↻ Probe bucket'}
          </button>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span className="phase">
            <span className={'dot ' + (health?.live ? 'live' : 'idle')} />
            {health === null
              ? 'PROBING…'
              : health.live
                ? `BUCKET LIVE — ${health.latency_ms} ms`
                : (health as any).auth
                  ? 'AUTH REQUIRED — unlock to verify bucket'
                  : 'UNREACHABLE'}
          </span>
          {health?.checked_utc && <span className="small dim">probed {fmtTime(health.checked_utc)} UTC{(health as any)?.checked_ist ? ` (${(health as any).checked_ist})` : ''}{health?.effective_source ? ` · via ${health.effective_source}` : ''}</span>}
          {health && !health.live && health.error && <span className="small" style={{ color: 'var(--bad)' }}>{health.error}</span>}
        </div>
        <div className="small dim" style={{ marginTop: 8 }}>
          Last poll: {fmtTime(st?.last_poll_utc)} · new objects: {(st?.new_since_start ?? 0).toLocaleString()}
          {st?.last_poll_utc && (() => {
            const ageSec = (Date.now() - new Date(st.last_poll_utc).getTime()) / 1000
            if (ageSec > 90) return <span style={{ color: 'var(--warn)', marginLeft: 8 }}>⚠ stale poll ({Math.floor(ageSec)}s ago — check /api/bucket-health)</span>
            return null
          })()}
        </div>
        <div className="small dim" style={{ marginTop: 4 }}>
          Live sync: <code className="mono">{st?.effective_source ?? st?.object_source ?? 'gcs'}</code> via <code className="mono">{st?.bucket_url ?? 'gs://…'}</code> · Watermarks <code className="mono">wm:&lt;prefix&gt;</code> + poll interval 10s hot / 900s cold-crawl (wrapping cursor) · Cold catch-up: <code className="mono">POST /api/verify</code> · Recon script: <code className="mono">scripts/reconcile_s3_gcs_shard.py</code>
        </div>
      </div>

      {/* Continuous S3 -> GCS mirror sync */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Continuous S3 → GCS mirror sync</span>
          <span className="phase" style={{ fontSize: 11, color: mirrorSync?.healthy ? 'var(--accent2)' : 'var(--warn)' }}>
            <span className={'dot ' + (mirrorSync?.healthy ? 'live' : 'idle')} />
            {mirrorSync?.healthy ? 'SYNCING' : (mirrorSync?.daemon_seen ? 'STALE' : 'NO DAEMON')}
          </span>
        </div>
        {mirrorSync ? (
          <>
            <div className="small" style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
              <span style={{ color: mirrorSync.healthy ? 'var(--good)' : 'var(--warn)' }}>{mirrorSync.verdict}</span>
              {mirrorSync.status_age_min != null && <span className="dim">last cycle {mirrorSync.status_age_min} min ago</span>}
              {mirrorSync.status?.cycle != null && <span className="dim">cycle #{mirrorSync.status.cycle}</span>}
              {mirrorSync.status?.uploaded_total != null && <span>uploaded <b>{mirrorSync.status.uploaded_total.toLocaleString()}</b></span>}
              {mirrorSync.status?.queued_pending != null && <span className="dim">pending {mirrorSync.status.queued_pending.toLocaleString()}</span>}
              {(mirrorSync.status?.scanned_this_cycle ?? mirrorSync.status?.scanned) != null && <span className="dim">scanned this cycle {(mirrorSync.status?.scanned_this_cycle ?? mirrorSync.status?.scanned ?? 0).toLocaleString()}</span>}
              {mirrorSync.status?.dashboard_notified != null && <span className="dim">fast-path indexed {mirrorSync.status.dashboard_notified.toLocaleString()}</span>}
              {mirrorSync.status?.errors ? <span style={{ color: 'var(--bad)' }}>errors {mirrorSync.status.errors}</span> : null}
              {mirrorSync.status?.dashboard_notify_errors ? <span style={{ color: 'var(--warn)' }}>notify fallbacks {mirrorSync.status.dashboard_notify_errors}</span> : null}
            </div>
            <div className="sync-metrics" style={{ marginTop: 10 }}>
              <div><span className="tiny dim">CURRENT S3 INVENTORY</span><strong>{mirrorSync.status?.source_inventory_objects?.toLocaleString() ?? 'building…'}</strong><small>{mirrorSync.status?.source_inventory_prefixes_scanned ?? 0}/{mirrorSync.status?.source_inventory_prefixes_total ?? '—'} prefixes measured</small></div>
              <div><span className="tiny dim">PRESERVED GCS MIRROR</span><strong>{mirrorSync.status?.gcs_mirror_objects?.toLocaleString() ?? '—'}</strong><small>includes retained historical objects</small></div>
              <div><span className="tiny dim">PRESERVED-ONLY ESTIMATE</span><strong>{mirrorSync.status?.preserved_only_estimate?.toLocaleString() ?? 'pending full inventory'}</strong><small>mirror minus latest complete source inventory</small></div>
              <div><span className="tiny dim">CAPTURE EXCEPTIONS</span><strong>{((mirrorSync.status?.tombstones_pending ?? 0) + (mirrorSync.status?.tombstones_terminal ?? 0)).toLocaleString()}</strong><small>{mirrorSync.status?.tombstones_pending ?? 0} retrying · {mirrorSync.status?.tombstones_terminal ?? 0} deleted-before-capture</small></div>
            </div>
            {mirrorSync.status?.source_inventory_oldest_scan_utc && <div className="tiny dim" style={{ marginTop: 7 }}>Inventory completeness: {mirrorSync.status.source_inventory_complete ? 'complete' : 'building'} · oldest prefix measurement {fmtTime(mirrorSync.status.source_inventory_oldest_scan_utc)} UTC</div>}
            <div className="tiny dim" style={{ marginTop: 6 }}>
              Daemon container <code className="mono">s3sync</code> mirrors the live S3 evidence bucket into <code className="mono">{mirrorSync.status?.gcs_target ?? st?.bucket_url ?? '…'}</code> with ISO-8601 custody metadata (sha256 + etag), so this dashboard stays real-time.
              {' '}{mirrorSync.status?.oversized_skipped ? `· ${mirrorSync.status.oversized_skipped.toLocaleString()} oversized objects skipped (already preserved)` : ''}
            </div>
          </>
        ) : (
          <div className="small dim">Probing sync daemon status…</div>
        )}
      </div>

      {/* Data types census */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Data types in bucket</span>
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
          <div className="small dim">Census loading…</div>
        )}
      </div>

      {/* Histogram */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Uploads — last 14 days (UTC)</span>
        </div>
        {stats && stats.day_histogram.some((d) => d.objects > 0) ? (
          <>
            <Histogram data={stats.day_histogram} />
            <div className="small dim" style={{ marginTop: 6 }}>
              Bars bucket by casino upload date (source last-modified), not mirror time.
              {(mirrorSync?.status?.queued_pending ?? 0) > 0
                ? ` Mirror backlog ${(mirrorSync?.status?.queued_pending ?? 0).toLocaleString()} pending — recent bars keep filling as the backlog drains.`
                : ' Mirror caught up — low bars mean the source went quiet, not a pipeline gap.'}
            </div>
          </>
        ) : (
          <div className="small dim">{st?.sweep_done ? 'No uploads in window.' : 'Waiting for sweep…'}</div>
        )}
      </div>

      {/* Sweep progress */}
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
            {Object.entries(st?.sweep_progress ?? {}).length}/{st?.prefixes.length ?? 28} prefixes · {swept.toLocaleString()} of {(st?.objects_total ?? 0).toLocaleString()} keys indexed
          </div>
        </div>
      )}

      {/* Prefix inventory */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Per-prefix inventory</span>
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

      {/* Enrichment / exhaustive decode */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Exhaustive decode — persistence</span>
          <button className="btn" onClick={refreshStats} style={{ fontSize: 11, padding: '4px 10px' }}>↻ Refresh</button>
        </div>
        <div className="small" style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          <span>Coverage: <b>{st?.enrichment?.coverage_pct != null ? `${st.enrichment.coverage_pct}%` : '—'}</b></span>
          <span>Pending enrich: <b>{st?.enrichment?.pending_now?.toLocaleString() ?? '—'}</b></span>
          <span>With payload: <b>{st?.enrichment?.with_payload ?? '—'}</b> / {(st?.enrichment?.total ?? 0).toLocaleString()}</span>
          <span>Enriched: <b>{st?.enrichment?.enriched_done ?? '—'}</b></span>
          <span>Last hour: <b>{st?.enrichment?.processed_last_hour?.toLocaleString() ?? '—'}</b></span>
          <span>ETA: <b>{st?.enrichment?.eta_hours != null ? `${st.enrichment.eta_hours.toLocaleString()}h` : 'calculating'}</b></span>
          <span>Errors: <b>{st?.enrichment?.errors?.toLocaleString() ?? '—'}</b></span>
          <span className="tiny dim">SQLite WAL · extract_json persisted · re-queue on etag/size change · payment_transactions materialized · WALLET_FULL_SCAN=0 (toggle via POST /api/wallets/full-scan)</span>
        </div>
        <div className="tiny dim" style={{ marginTop: 6 }}>Objects re-decode only when size/ETag/timestamp changes. Completion requires pending = 0 and errors reviewed; compare <code className="mono">GET /api/types</code> counts with money-flow coverage before reporting totals as exhaustive.</div>
      </div>

      {/* Network URLs — every frontend/backend/other URL in the network + CSV */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Network URLs ({(netUrls?.counts?.total ?? 0).toLocaleString()} hosts)</span>
          <button className="btn" onClick={exportNetCsv} disabled={!netUrls?.rows?.length} style={{ fontSize: 11, padding: '4px 10px' }}>
            ⬇ Export table CSV ({(netUrls?.rows ?? []).length})
          </button>
        </div>
        <div className="small dim" style={{ marginBottom: 8 }}>
          gateway-brand = white-label frontends from settlement exports · qr-payload / qr-expanded = URLs inside collection QR codes ·
          showing {(netUrls?.rows ?? []).slice(0, 100).length} of {(netUrls?.rows ?? []).length} · CSV carries every row
        </div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr>
          <th>Host</th><th>Source</th><th className="num">Hits</th><th className="num">Amount</th><th>Sample URL</th><th>Evidence key</th>
        </tr></thead>
          <tbody>{(netUrls?.rows ?? []).slice(0, 100).map((r) => <tr key={r.source + ':' + r.host}>
            <td className="mono" style={{ fontWeight: 700 }}>{r.host}</td>
            <td className="small">{r.source}</td>
            <td className="num">{r.hits.toLocaleString()}</td>
            <td className="num money">{r.source === 'gateway-brand' ? fmtINR(r.amount_inr) : '—'}</td>
            <td className="mono small" style={{ maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.url}>{r.url}</td>
            <td className="mono small" title={r.sample_key}>{r.sample_key ? r.sample_key.split('/').pop() : '—'}</td>
          </tr>)}{!netUrls && <tr><td colSpan={6} className="dim">Loading network inventory…</td></tr>}</tbody></table></div>
      </div>

      {/* Runtime */}
      <div className="st-panel" style={{ marginBottom: 0 }}>
        <div className="st-panel-head">
          <span className="st-panel-title">Monitor runtime</span>
        </div>
        <div className="small" style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <span>Started <b>{fmtTime(st?.started_utc)}</b></span>
          <span>Last poll <b>{fmtTime(st?.last_poll_utc)}</b></span>
          <span>Bucket <b style={{ color: st?.bucket_live ? 'var(--accent2)' : 'var(--bad)' }}>{String(st?.bucket_live ?? '?')}</b></span>
        </div>
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
