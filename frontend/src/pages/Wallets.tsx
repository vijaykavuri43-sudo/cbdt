import { useCallback, useEffect, useRef, useState } from 'react'
import { api, createRefreshGuard, eventsUrl, exportWalletsUrl, objectUrl, post, saveCsvFromUrl, tabHidden } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl, firstTxid, traceDeskAddressUrl, tronscanLabel, tronscanTxUrl } from '../lib/explorers'
import { fmtIST, fmtMoney } from '../lib/format'

// Every row is one address decoded from a live bucket image (QR / slip /
// crypto chat screenshot). Origin is ALWAYS the exact S3 bucket object key —
// so every row has view proof. No external reference lists.
interface WalletRow {
  key: string
  last_modified: string
  address?: string
  raw_address?: string
  wallet_status?: string
  network?: string
  standard?: string
  confidence?: string
  role?: string | null
  role_confidence?: string | null
  checksum?: string | null
  ocr_recovered?: boolean
  amount_inr?: number | null
  amount_value?: number | null
  amount_currency?: string | null
  txids?: string[] | string | null
  txid?: string
  tx_status?: string
  chain_checked_utc?: string
  kind?: string | null
  hits?: number
  first_seen?: string
  last_seen?: string
  known_operator?: boolean
}
interface WalletsResp {
  items: WalletRow[]
  wallet_hits_total: number
  distinct_wallets: number
  by_network: { network: string; count: number }[]
  extraction_status?: { enriched: number; pending: number; pending_live?: number; slip_backlog?: number; objects: number; pending_full_scan?: number }
  source?: string
  distinct?: boolean
  limit?: number
  offset?: number
  total?: number
}
interface RefWallet {
  address: string
  network?: string
  category?: string
  attribution?: string
  role?: string
  confidence?: string
  evidence_type?: string
  source_ref?: string
  proof_s3_key?: string
  proof_url?: string
  fact_type?: string
  in_live?: boolean
  in_mirror?: boolean | null
  le_master?: boolean
  label?: string
  case_ids?: string
  notes?: string
  le_confidence?: string
  le_fact_type?: string
  first_seen?: string
  last_seen?: string
  source_path?: string
  source_folder?: string
  all_source_files?: string[]
}
interface RefResp {
  items: RefWallet[]
  identified_total: number
  live_decoded: number
  missing_from_live: number
  le_master_total?: number
}

// Origin shows folder + file (folder suffix), never the bare basename:
// "withdrawal/1763345228102.jpg" — full key stays in the title tooltip.
function originShort(key: string): string {
  const parts = (key || '').split('/').filter(Boolean)
  if (parts.length <= 1) return key || '—'
  return `${parts[0]}/${parts[parts.length - 1]}`
}

// On-chain transaction attached to this wallet hit by the Tronscan verifier.
// "derived" means the hash was reconstructed from an OCR fragment and then
// confirmed on-chain; "ambiguous" keeps every candidate as a lead, not a fact.
function WalletTxCell({ row }: { row: WalletRow }) {
  const status = (row.tx_status || '').toLowerCase()
  const tx = firstTxid(row.txid) || firstTxid(row.txids)
  const label = status || 'unchecked'
  const title =
    status === 'verified'
      ? 'Full transaction hash read from the slip and confirmed on Tronscan.'
      : status === 'derived'
        ? 'Transaction hash reconstructed from the OCR fragment and confirmed on-chain.'
        : status === 'ambiguous'
          ? 'Several on-chain transactions fit this slip; candidates are leads, not facts.'
          : status === 'unresolved'
            ? 'No unique on-chain transaction could be proven from this image.'
            : 'Not yet checked against Tronscan.'
  return (
    <div className="oq-tx-cell">
      {tx ? (
        <a className="mono small oq-txid" href={tronscanTxUrl(tx)} target="_blank" rel="noreferrer" title={tx}>
          {tx.slice(0, 10)}…{tx.slice(-8)}
        </a>
      ) : (
        <span className="tiny dim">no hash</span>
      )}
      <span className={`chain-badge chain-${status || 'unchecked'}`} title={title}>
        {label}
      </span>
    </div>
  )
}
interface FullScanStatus {
  running: boolean
  scanned: number
  found: number
  errors: number
  started_utc: string | null
  last_key: string | null
  limit: number
  rate_per_sec: number
  pending_full_scan: number
  pending_live: number
  source: string
  gcs_bucket: string
  gcs_prefix: string
}

export function Wallets() {
  const PAGE_SIZE = 500
  const [data, setData] = useState<WalletsResp | null>(null)
  const [scan, setScan] = useState<FullScanStatus | null>(null)
  const [distinct, setDistinct] = useState(true)
  const [page, setPage] = useState(0)
  const [error, setError] = useState('')
  const [ref, setRef] = useState<RefResp | null>(null)
  const [refError, setRefError] = useState('')
  const [q, setQ] = useState('')
  const [qDebounced, setQDebounced] = useState('')
  const [network, setNetwork] = useState('')
  const [sort, setSort] = useState('recent')
  const [dlBusy, setDlBusy] = useState<'' | 'hits' | 'distinct' | 'reference'>('')
  const [popup, setPopup] = useState<'' | 'hits' | 'distinct' | 'reference'>('')
  const [popupHits, setPopupHits] = useState<WalletRow[]>([])
  const [popupRef, setPopupRef] = useState<RefWallet[]>([])
  const [popupErr, setPopupErr] = useState('')
  const [popupBusy, setPopupBusy] = useState(false)

  const refresh = useCallback(() => {
    // 2026-09-16: paginated — old wallets never fall off the newest-N window.
    // Distinct mode (default) gives one row per address so old single-hit
    // wallets stay reachable; All-hits mode shows every object×wallet row.
    // 2026-09-17: q/network filters + distinct sort are server-side.
    setError('')
    return api<WalletsResp>('/api/wallets', { limit: PAGE_SIZE, offset: page * PAGE_SIZE, distinct: distinct ? 1 : 0, q: qDebounced, network, sort }, 60000)
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [page, distinct, qDebounced, network, sort])
  const refreshScan = useCallback(() => {
    api<FullScanStatus>('/api/wallets/full-scan/status').then(setScan).catch(() => {})
  }, [])
  useEffect(() => { refresh() }, [refresh])
  useEffect(() => { setPage(0) }, [distinct])
  // Debounce the address/source search so each keystroke isn't a request;
  // any filter change restarts at page 1.
  useEffect(() => {
    const id = setTimeout(() => setQDebounced(q.trim()), 400)
    return () => clearTimeout(id)
  }, [q])
  useEffect(() => { setPage(0) }, [qDebounced, network, sort])
  const loadRef = useCallback(() => {
    setRefError('')
    api<RefResp>('/api/wallets/reference', {}, 60000).then(setRef).catch((e) => setRefError(String(e)))
  }, [])
  useEffect(loadRef, [loadRef])
  // Storm-guarded live nudges (2026-09-16): SSE bursts share one guard with
  // the scan poller — no concurrent /api/wallets pile-ups.
  const guard = useRef(createRefreshGuard(20000))
  useEffect(() => {
    refreshScan()
    const id = setInterval(() => { if (!tabHidden()) refreshScan() }, 3000)
    // live updates via SSE wallet_scan_progress
    let es: EventSource | null = null
    try {
      es = new EventSource(eventsUrl())
      es.onmessage = (ev) => {
        try {
          const j = JSON.parse(ev.data)
          if (j.type === 'wallet_scan_progress' || j.type === 'wallet_scan_done' || j.type === 'enriched') {
            refreshScan()
            if (j.type === 'wallet_scan_done' || j.type === 'enriched') guard.current.nudge(refresh)
          }
        } catch {}
      }
    } catch {}
    return () => {
      clearInterval(id)
      if (es) es.close()
    }
  }, [refresh, refreshScan])

  async function startFullScan() {
    if (!confirm(`Full-bucket wallet scan: prioritized QR → slips → all chat_file images directly against GCS s3-mirror (${data?.source ?? 'gs://cbtf-case-evidence-518405669914/s3-mirror/'}). 50k objects ≈ 20–40 min at 6 workers. Continue?`)) return
    try {
      await post('/api/wallets/full-scan?limit=50000&concurrency=6')
      refreshScan()
    } catch (e: any) {
      alert(e?.message || String(e))
    }
  }

  // Rows missing a bucket key carry no proof — keep them out of the exhibit.
  const rows = (data?.items ?? []).filter((i) => (i.key || '').trim() !== '')
  const total = data?.total ?? rows.length
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1
  const to = Math.min(total, page * PAGE_SIZE + rows.length)

  async function downloadKind(kind: 'hits' | 'distinct' | 'reference', filename: string) {
    if (dlBusy) return
    setDlBusy(kind)
    try {
      await saveCsvFromUrl(exportWalletsUrl(kind), filename)
    } catch (e: any) {
      alert(e?.message || String(e))
    } finally {
      setDlBusy('')
    }
  }

  async function openPopup(kind: 'hits' | 'distinct' | 'reference') {
    setPopup(kind)
    setPopupErr('')
    setPopupHits([])
    setPopupRef([])
    setPopupBusy(true)
    try {
      if (kind === 'reference') {
        const r = ref ?? await api<RefResp>('/api/wallets/reference', {}, 60000)
        if (!ref) setRef(r)
        setPopupRef(r.items ?? [])
      } else {
        const r = await api<WalletsResp>('/api/wallets', { limit: 2000, offset: 0, distinct: kind === 'distinct' ? 1 : 0 }, 60000)
        setPopupHits((r.items ?? []).filter((i) => (i.key || '').trim() !== '' || (i.address || '').trim() !== ''))
      }
    } catch (e: any) {
      setPopupErr(e?.message || String(e))
    } finally {
      setPopupBusy(false)
    }
  }

  useEffect(() => {
    if (!popup) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setPopup('') }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [popup])

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">₿</span>
          <div>
            <h1 className="pg-header-title">Crypto Wallets</h1>
            <div className="pg-header-sub">
              Source: <span className="mono">{data?.source ?? 'gs://cbtf-case-evidence-518405669914/s3-mirror/'}</span> · wallets parsed from deposit / withdrawal / banking QR / payment / chat screenshots · click a card to open all rows
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={refresh} style={{ fontSize: 11, marginRight: 8 }}>↻ Refresh</button>
          <button className="btn" onClick={startFullScan} style={{ fontSize: 11, borderColor: scan?.running ? 'var(--accent)' : undefined, opacity: scan?.running ? 0.6 : 1 }} disabled={!!scan?.running}>{scan?.running ? `⏳ scanning ${scan.scanned.toLocaleString()}` : '◉ Full wallet scan (GCS)'}</button>
        </div>
      </div>

      {scan && (
        <div className="dp-table-wrap" style={{ marginBottom: 14, borderStyle: scan.running ? 'solid' : 'dashed', opacity: scan.running ? 1 : 0.95 }}>
          <div style={{ padding: '8px 12px', display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center', fontSize: 11 }}>
            <span><b>Full-bucket wallet scan</b> · source <span className="mono">{scan.gcs_bucket}/{scan.gcs_prefix}</span></span>
            <span className={scan.running ? 'ex-badge ex-badge-new' : 'ex-badge'}>{scan.running ? 'RUNNING' : 'idle'}</span>
            <span>scanned <b>{scan.scanned.toLocaleString()}</b> / {scan.limit.toLocaleString()} · found <b>{scan.found}</b> · {scan.rate_per_sec}/s</span>
            <span className="small dim">pending full-scan {scan.pending_full_scan.toLocaleString()} · pending live {scan.pending_live.toLocaleString()}</span>
            {scan.last_key && <span className="small dim mono" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={scan.last_key}>{scan.last_key}</span>}
            {scan.started_utc && <span className="small dim">since {scan.started_utc}</span>}
          </div>
        </div>
      )}

      <div className="st-cards" style={{ marginBottom: 14 }}>
        <button
          type="button"
          className="st-card st-card-accent st-card-click"
          disabled={popupBusy}
          title="Open every bucket-decoded image×wallet row on this page"
          onClick={() => void openPopup('hits')}
        >
          <div className="st-card-label">Wallet hits (bucket-decoded)</div>
          <div className="st-card-value accent">{(data?.wallet_hits_total ?? rows.length).toLocaleString()}</div>
          <div className="st-card-sub">
            {popup === 'hits' && popupBusy ? 'Opening…' : `click to view ${(data?.wallet_hits_total ?? 0).toLocaleString()} rows · each hit = one source file`}
          </div>
        </button>
        <button
          type="button"
          className="st-card st-card-click"
          disabled={popupBusy}
          title="Open one row per distinct address with all source files"
          onClick={() => void openPopup('distinct')}
        >
          <div className="st-card-label">Distinct addresses</div>
          <div className="st-card-value">{(data?.distinct_wallets ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">
            {popup === 'distinct' && popupBusy
              ? 'Opening…'
              : `click to view ${(data?.distinct_wallets ?? 0).toLocaleString()} unique wallets · all source files + proof`}
          </div>
        </button>
        <div className="st-card">
          <div className="st-card-label">GCS Enrichment (live source)</div>
          <div className="st-card-value small">{data?.extraction_status ? `${(data.extraction_status.enriched ?? 0).toLocaleString()} / ${(data.extraction_status.objects ?? 0).toLocaleString()}` : '—'}</div>
          <div className="st-card-sub">pending full-scan {data?.extraction_status?.pending_full_scan?.toLocaleString() ?? '—'} · scan covers all images, not just hints</div>
        </div>
      </div>

      {data && data.by_network.length > 0 && (
        <div style={{ marginBottom: 12 }} className="small dim">
          By network:{' '}
          {data.by_network.map((n) => (
            <span key={n.network} className="mono" style={{ marginRight: 12 }}>
              {n.network}: <b>{n.count.toLocaleString()}</b>
            </span>
          ))}
        </div>
      )}

      <div className="lf-controls">
        <div className="lf-controls-left">
          <button className={`ex-toggle ${distinct ? 'ex-toggle-on' : ''}`} onClick={() => setDistinct((d) => !d)} title="One row per unique address with hit count — the officer action list; reaches old single-hit wallets">
            ⇄ {distinct ? 'Unique wallets ✓' : 'Group: unique wallets'}
          </button>
          <button className={`ex-toggle ${!distinct ? 'ex-toggle-on' : ''}`} onClick={() => setDistinct(false)} title={`Every object×wallet hit row across all pages (${(data?.wallet_hits_total ?? 0).toLocaleString()} rows, ${PAGE_SIZE} per page)`}>All hits · {(data?.wallet_hits_total ?? 0).toLocaleString()}</button>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search address / source file…"
            title="Substring match on wallet address or bucket source key (server-side)"
            style={{ fontSize: 11, padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border, #444)', background: 'transparent', color: 'inherit', minWidth: 200 }}
          />
          <select
            value={network}
            onChange={(e) => setNetwork(e.target.value)}
            title="Filter by network"
            style={{ fontSize: 11, padding: '4px 6px', borderRadius: 6, border: '1px solid var(--border, #444)', background: 'transparent', color: 'inherit' }}
          >
            <option value="">All networks</option>
            {(data?.by_network ?? []).map((n) => (
              <option key={n.network} value={n.network}>{n.network} ({n.count.toLocaleString()})</option>
            ))}
          </select>
          {distinct && (
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value)}
              title="Sort unique wallets"
              style={{ fontSize: 11, padding: '4px 6px', borderRadius: 6, border: '1px solid var(--border, #444)', background: 'transparent', color: 'inherit' }}
            >
              <option value="recent">Recent first</option>
              <option value="hits">Most hits</option>
              <option value="amount">Largest amount</option>
            </select>
          )}
          {(qDebounced || network) && (
            <button className="ex-toggle" onClick={() => { setQ(''); setQDebounced(''); setNetwork('') }} title="Clear search + network filter">✕ clear</button>
          )}
          <span className="small dim">All rows: bucket-decoded wallets — exact source file + view proof + Arkham + Tronscan + TraceDesk.</span>
        </div>
        <span className="small dim">Live hits: QR/slip/chat extraction; nil right now is backlog — {(data?.extraction_status?.pending ?? 0).toLocaleString()} undecoded ({(data?.extraction_status?.pending_live ?? 0).toLocaleString()} live queue + {(data?.extraction_status?.slip_backlog ?? 0).toLocaleString()} older slips).</span>
      </div>
      {error && <div className="money-error">Could not load crypto wallets: {error} <button className="btn" style={{ marginLeft: 8 }} onClick={refresh}>Retry</button></div>}

      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
              <tr>
                <th>Address</th>
                <th>Network</th>
                <th>Tx hash</th>
                <th>Label</th>
                <th>Source</th>
                <th>{distinct ? 'First seen' : 'Object time'}</th>
                <th>Detail</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r: any) => (
              <tr key={r.key + (r.address ?? '')}>
                <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>{r.address}{r.checksum === 'invalid' && <span title="On-chain checksum failed — OCR-degraded read, treat as unverified"> ⚠</span>}
                  {r.raw_address && r.raw_address !== r.address && (
                    <div className="tiny dim mono" title="Raw OCR value (preserved as evidence)">OCR: {r.raw_address}</div>
                  )}
                </td>
                <td>{r.network ?? '—'}</td>
                <td><WalletTxCell row={r} /></td>
                <td className="small">{r.standard ?? '—'}</td>
                <td className="small dim mono" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key || ''}>
                  {originShort(r.key ?? '')}
                  {r.key?.includes('/') && (
                    <div className="tiny dim">{r.key.split('/')[0]}</div>
                  )}
                </td>
                <td className="small" title={distinct ? `First ${r.first_seen || '—'} UTC · last ${r.last_seen || '—'} UTC` : `${r.last_modified || ''} UTC`}>
                  {fmtIST(distinct ? r.first_seen : r.last_modified)}
                  {distinct && r.last_seen && r.last_seen !== r.first_seen && (
                    <div className="tiny dim">last {fmtIST(r.last_seen)}</div>
                  )}
                </td>
                <td className="small dim" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.role === 'sender' ? 'From · sender' : r.role === 'beneficiary' ? 'To · recipient' : ''}>
                  {fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency ?? (r.amount_inr != null ? 'INR' : null))}
                </td>
                <td>
                  <span className="ex-badge" title="Extraction confidence from the bucket image">{r.confidence || 'EXTRACTED'}{r.ocr_recovered ? ' · OCR' : ''}</span>
                  {r.hits != null && <span className="ex-badge" title={`Seen in ${r.hits} bucket object(s) · ${r.first_seen ?? ''} → ${r.last_seen ?? ''}`} style={{ marginLeft: 6 }}>{r.hits}× hits</span>}
                  {r.known_operator && <span className="ex-badge" style={{ marginLeft: 6, borderColor: 'var(--accent2)', color: 'var(--accent2)' }} title="Address is on the LE Submission Master operator list">known operator</span>}
                </td>
                 <td style={{ whiteSpace: 'nowrap' }}><a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view proof</a>{' · '}{r.address && <><a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(r.address, r.txids) ?? '#'} target="_blank" rel="noreferrer" className="small">{tronscanLabel(r.txids)}</a>{' · '}<a href={traceDeskAddressUrl(r.address)} target="_blank" rel="noopener noreferrer" className="small">TraceDesk ↗</a></>}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={9} className="dp-empty">
                  <div className="dp-empty-icon">₿</div>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>No wallets yet on this monitor host</div>
                  <div className="small dim" style={{ maxWidth: 720, margin: '0 auto', lineHeight: 1.5 }}>
                    This monitor extracts wallets live from <span className="mono">bankingqr/</span>, <span className="mono">qrcode/</span>, <span className="mono">payment/</span>, <span className="mono">deposit/</span>, <span className="mono">withdrawal/</span> and crypto-named <span className="mono">chat_file/</span> screenshots. On this host enrichment is {data?.extraction_status ? `${data.extraction_status.enriched?.toLocaleString()} of ${data.extraction_status.objects?.toLocaleString()} (${(data.extraction_status.pending ?? 0).toLocaleString()} undecoded)` : 'still draining'} — so nil is the backlog, not the data.
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
        <button className="btn" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>← Prev</button>
        <button className="btn" disabled={page + 1 >= pageCount} onClick={() => setPage((p) => p + 1)}>Next →</button>
        <span className="small dim">Page {page + 1} of {pageCount} · showing {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()} {distinct ? 'unique wallets' : 'hits'}{(qDebounced || network) ? ` matching${qDebounced ? ` “${qDebounced}”` : ''}${network ? ` · ${network}` : ''}` : ''} (all pages reachable — old wallets included)</span>
      </div>

      <div className="pg-header" style={{ marginTop: 26 }}>
        <div className="pg-header-left">
          <span className="pg-header-icon">📋</span>
          <div>
            <h1 className="pg-header-title">Identified wallets (reference)</h1>
            <div className="pg-header-sub">
              {ref
                ? <>All {ref.identified_total.toLocaleString()} previously-identified wallets — hawala, operator, beneficiary, desk. CASE-2026-102 operator wallets ({ref.le_master_total ?? 0}) listed first. {ref.live_decoded.toLocaleString()} also live-decoded above · {ref.missing_from_live.toLocaleString()} reference-only.</>
                : refError
                  ? <>Reference list failed to load — press Retry below. Live bucket-decoded wallets above are unaffected.</>
                  : <>Loading the previously-identified wallet census…</>}
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button
            className="btn"
            disabled={popupBusy}
            style={{ fontSize: 11 }}
            title="Open the identified census on this page"
            onClick={() => void openPopup('reference')}
          >
            {popup === 'reference' && popupBusy ? 'Opening…' : `View census (${(ref?.identified_total ?? 0).toLocaleString()})`}
          </button>
        </div>
      </div>

      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
            <tr>
              <th>Address</th>
              <th>Network</th>
              <th>Role</th>
              <th>Attribution</th>
              <th>First seen</th>
              <th>Source</th>
              <th>Proof</th>
              <th>Live</th>
            </tr>
          </thead>
          <tbody>
            {(ref?.items ?? []).map((w) => (
              <tr key={w.address} style={w.le_master ? { background: 'rgba(46,184,126,0.06)' } : undefined}>
                <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>
                  {w.address}
                </td>
                <td>{w.network ?? '—'}</td>
                <td className="small" title={w.notes || ''}>{w.label || w.role || '—'}</td>
                <td className="small">{w.attribution || w.notes || '—'}</td>
                <td className="small" title={w.first_seen ? `${w.first_seen} UTC` : 'Not yet decoded from a live bucket image'}>
                  {w.first_seen ? fmtIST(w.first_seen) : '—'}
                  {w.last_seen && w.last_seen !== w.first_seen && (
                    <div className="tiny dim">last {fmtIST(w.last_seen)}</div>
                  )}
                </td>
                <td className="small dim mono" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={w.source_path || w.source_ref || ''}>
                  {w.source_path ? originShort(w.source_path) : '—'}
                  {w.source_folder && (
                    <div className="tiny dim">{w.source_folder}</div>
                  )}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {w.in_mirror && w.proof_s3_key
                    ? <a href={objectUrl(w.proof_s3_key)} target="_blank" rel="noreferrer" className="small">view proof</a>
                    : w.proof_s3_key
                      ? <span className="small dim" title={`True source key ${w.in_mirror === false ? 'deleted from the bucket before the mirror captured it — no proof available' : 'not yet indexed on this host'}`}>{w.in_mirror === false ? 'deleted' : 'pending'}</span>
                      : <span className="small dim" title="Report citation recorded no bucket object (spreadsheet cell, dashboard snapshot, or chat reference) — not a bucket source">no bucket key</span>}
                   {w.address && <>{' · '}<a href={arkhamAddressUrl(w.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(w.address, null) ?? '#'} target="_blank" rel="noreferrer" className="small">Tronscan ↗</a>{' · '}<a href={traceDeskAddressUrl(w.address)} target="_blank" rel="noopener noreferrer" className="small">TraceDesk ↗</a></>}
                </td>
                <td>
                  {w.in_live
                    ? <span className="ex-badge" title="Also decoded from a live bucket image in the table above">live ✓</span>
                    : <span className="ex-badge ex-badge-new" title="Not decoded from any bucket image currently in the mirror — source object is a spreadsheet/config or outside the mirror">ref-only</span>}
                </td>
              </tr>
            ))}
            {(ref?.items ?? []).length === 0 && (
              <tr>
                <td colSpan={8} className="dp-empty">
                  <div className="dp-empty-icon">📋</div>
                  {refError
                    ? <><div style={{ fontWeight: 700, marginBottom: 4 }}>Could not load reference wallets: {refError}</div><button className="btn" onClick={loadRef}>Retry</button></>
                    : <div style={{ fontWeight: 700, marginBottom: 4 }}>Loading reference wallets…</div>}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="small dim" style={{ marginTop: 8 }}>
        Attribution labels are report attributions, not proof of ownership/control — corroborate via Tronscan history, VASP/KYC, banking records, device evidence.
      </div>

      {popup && (
        <div className="modal-backdrop" onClick={() => setPopup('')}>
          <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 700 }}>
                  {popup === 'hits' && `Wallet hits — ${(popupBusy ? data?.wallet_hits_total : popupHits.length)?.toLocaleString() ?? '…'} rows`}
                  {popup === 'distinct' && `Distinct wallets — ${(popupBusy ? data?.distinct_wallets : popupHits.length)?.toLocaleString() ?? '…'} addresses`}
                  {popup === 'reference' && `Identified census — ${(popupBusy ? ref?.identified_total : popupRef.length)?.toLocaleString() ?? '…'} wallets`}
                </div>
                <div className="small dim">
                  {popup === 'hits' && 'Every bucket-decoded image × wallet. Source is deposit / withdrawal / chat / bankingqr.'}
                  {popup === 'distinct' && 'One row per live address. Source is the bucket object path.'}
                  {popup === 'reference' && 'Previously-identified reference list. Source is the bucket object path, not /api/wallets.'}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                <button
                  className="btn"
                  disabled={!!dlBusy || popupBusy}
                  onClick={() => void downloadKind(popup, `crypto-wallets-${popup}-${new Date().toISOString().slice(0, 10)}.csv`)}
                >
                  {dlBusy === popup ? 'Preparing CSV…' : '⬇ Download CSV'}
                </button>
                <button className="btn btn-primary" onClick={() => setPopup('')}>Close <span className="kbd" style={{ marginLeft: 4 }}>Esc</span></button>
              </div>
            </div>
            <div className="modal-body" style={{ padding: 0 }}>
              {popupErr && <div className="money-error" style={{ margin: 12 }}>{popupErr}</div>}
              {popupBusy && <div className="small dim" style={{ padding: 18 }}>Loading all rows…</div>}
              {!popupBusy && popup !== 'reference' && (
                <div className="dp-table-wrap" style={{ maxHeight: '70vh', overflow: 'auto' }}>
                  <table className="dp-table">
                    <thead>
                      <tr>
                        <th>Address</th>
                        <th>Network</th>
                        <th>Label</th>
                        <th>Source</th>
                        <th>{popup === 'distinct' ? 'First seen' : 'Object time'}</th>
                        <th>Detail</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {popupHits.map((r) => (
                        <tr key={(r.key || '') + (r.address ?? '') + (r.last_modified || '')}>
                          <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>{r.address}</td>
                          <td>{r.network ?? '—'}</td>
                          <td className="small">{r.standard ?? '—'}</td>
                          <td className="small dim mono" title={r.key || ''}>
                            {originShort(r.key ?? '')}
                            {r.key?.includes('/') && <div className="tiny dim">{r.key.split('/')[0]}</div>}
                          </td>
                          <td className="small">{fmtIST(popup === 'distinct' ? r.first_seen : r.last_modified)}</td>
                          <td className="small dim">{fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency ?? (r.amount_inr != null ? 'INR' : null))}{r.hits != null ? ` · ${r.hits}×` : ''}</td>
                          <td style={{ whiteSpace: 'nowrap' }}>
                            {r.key && <a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view proof</a>}
                             {r.address && <>{' · '}<a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(r.address, null) ?? '#'} target="_blank" rel="noreferrer" className="small">Tronscan ↗</a>{' · '}<a href={traceDeskAddressUrl(r.address)} target="_blank" rel="noopener noreferrer" className="small">TraceDesk ↗</a></>}
                          </td>
                        </tr>
                      ))}
                      {popupHits.length === 0 && (
                        <tr><td colSpan={7} className="dp-empty">No rows</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}
              {!popupBusy && popup === 'reference' && (
                <div className="dp-table-wrap" style={{ maxHeight: '70vh', overflow: 'auto' }}>
                  <table className="dp-table">
                    <thead>
                      <tr>
                        <th>Address</th>
                        <th>Network</th>
                        <th>Role</th>
                        <th>First seen</th>
                        <th>Source</th>
                        <th>Live</th>
                      </tr>
                    </thead>
                    <tbody>
                      {popupRef.map((w) => (
                        <tr key={w.address}>
                          <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>
                            {w.address}
                            {w.address && <div className="tiny" style={{ fontWeight: 400 }}><a href={arkhamAddressUrl(w.address)} target="_blank" rel="noreferrer">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(w.address, null) ?? '#'} target="_blank" rel="noreferrer">Tronscan ↗</a>{' · '}<a href={traceDeskAddressUrl(w.address)} target="_blank" rel="noopener noreferrer">TraceDesk ↗</a></div>}
                          </td>
                          <td>{w.network ?? '—'}</td>
                          <td className="small">{w.label || w.role || '—'}</td>
                          <td className="small">{w.first_seen ? fmtIST(w.first_seen) : '—'}</td>
                          <td className="small dim mono" title={w.source_path || w.source_ref || ''}>
                            {w.source_path ? originShort(w.source_path) : '—'}
                            {w.source_folder && <div className="tiny dim">{w.source_folder}</div>}
                          </td>
                          <td>{w.in_live ? 'live ✓' : 'ref-only'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
