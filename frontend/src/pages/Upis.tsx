import { useCallback, useEffect, useState } from 'react'
import { api, exportUpisUrl, objectUrl } from '../lib/api'
import { fmtINR, fmtTime } from '../lib/format'

interface UpiRow {
  key: string; last_modified: string; vpa?: string; payee_name?: string; amount_inr?: number | null; known_mule?: boolean; raw_text?: string | null; kind?: string; origin?: string
  slips?: number; total_inr?: number | null; with_amount?: number; first_seen?: string; last_seen?: string
  sender_only?: boolean; has_sender?: number; has_bene?: number
}

function folderOf(key: string): string {
  const i = key.indexOf('/')
  return i >= 0 ? key.slice(0, i + 1) : '(root)'
}

export function Upis() {
  const PAGE_SIZE = 500
  const [data, setData] = useState<{ master_size: number; items: UpiRow[]; new_not_in_master: number; new_total?: number; total?: number } | null>(null)
  const [onlyNew, setOnlyNew] = useState(false)
  const [distinct, setDistinct] = useState(true)
  const [hideSenders, setHideSenders] = useState(true)
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<UpiRow | null>(null)
  const [imgErr, setImgErr] = useState(false)

  const refresh = useCallback(() => {
    api<any>('/api/upis', { limit: PAGE_SIZE, offset: page * PAGE_SIZE, distinct: distinct ? 1 : 0, new_only: onlyNew ? 1 : 0, hide_senders: hideSenders ? 1 : 0 }).then(setData).catch(() => {})
  }, [distinct, onlyNew, hideSenders, page])
  useEffect(refresh, [refresh])
  useEffect(() => { setImgErr(false) }, [selected?.key])
  // Newest-first feed grows over time — jumping pages re-syncs; toggles restart at page 0
  useEffect(() => { setPage(0) }, [distinct, onlyNew, hideSenders])

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') setSelected(null) }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  const items = data?.items ?? []
  const total = data?.total ?? items.length
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1
  const to = Math.min(total, page * PAGE_SIZE + items.length)
  const seen = new Set<string>()

  function exportTsv() {
    const header = 'VPA\tPayee Name\tAmount (INR)\tStatus\tSource File\tSource Folder\tQR Uploaded\n'
    const rows = items.map((r) => [r.vpa, r.payee_name ?? '', r.amount_inr ?? '', r.known_mule ? 'known' : 'NEW', r.key, folderOf(r.key), r.last_modified].join('\t')).join('\n')
    const blob = new Blob([header + rows], { type: 'text/tab-separated-values' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `upi-mules-${new Date().toISOString().slice(0, 10)}.tsv`; a.click()
  }

  function exportFullCsv() {
    // Full officer exhibit: every decoded VPA (old + new) with source file +
    // dashboard link per row — server streams the whole list as CSV (Excel-ready)
    const a = document.createElement('a')
    a.href = exportUpisUrl()
    a.download = `decoded-vpas-full-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">@</span>
          <div>
            <h1 className="pg-header-title">UPI / Mule Accounts</h1>
            <div className="pg-header-sub">Mule VPAs — unique collection rails with deposit counts · sender-side (user) VPAs hidden · cross-checked against {(data?.master_size ?? 0).toLocaleString()} known mules</div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={exportFullCsv} title="Full VPA list (old + new) with source file + dashboard link per row — officer exhibit, opens in Excel" style={{ fontSize: 11, marginRight: 8 }}>⬇ Export FULL CSV (LE)</button>
          <button className="btn" onClick={exportTsv} style={{ fontSize: 11 }}>⬇ Export ({items.length})</button>
        </div>
      </div>

      <div className="lf-controls">
        <div className="lf-controls-left">
          <button className={`ex-toggle ${!onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(false)}>All · {data?.items.length ?? 0}</button>
          <button className={`ex-toggle ${onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(true)}>
            ⚠ NEW only · {(data?.new_total ?? data?.new_not_in_master ?? 0).toLocaleString()}
          </button>
          <button className={`ex-toggle ${distinct ? 'ex-toggle-on' : ''}`} onClick={() => setDistinct((d) => !d)} title="One row per unique VPA with slip count + summed amount — the officer action list">
            ⇄ {distinct ? 'Unique VPAs' : 'Per-slip rows'}
          </button>
          <button className={`ex-toggle ${hideSenders ? 'ex-toggle-on' : ''}`} onClick={() => setHideSenders((h) => !h)} title="Sender-only VPAs are gambler/user counterparties (Paid-by side), not mule rails — hidden by default">
            🚫 {hideSenders ? 'Senders hidden (users out)' : 'Show senders (users)'}
          </button>
        </div>
      </div>

      <div className="st-cards" style={{ marginBottom: 16 }}>
        <div className="st-card st-card-accent">
              <div className="st-card-label">{distinct ? 'Unique VPAs (all pages)' : 'Slip rows (all pages)'}</div>
          <div className="st-card-value accent">{total.toLocaleString()}</div>
          <div className="st-card-sub">Page {page + 1} of {pageCount} · showing {from.toLocaleString()}–{to.toLocaleString()}</div>
        </div>
        <div className="st-card st-card-warn">
          <div className="st-card-label">New — not in master</div>
          <div className="st-card-value warn">{(data?.new_total ?? data?.new_not_in_master ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">Register in registry</div>
        </div>
        <div className="st-card">
          <div className="st-card-label">Master list</div>
          <div className="st-card-value">{(data?.master_size ?? 0).toLocaleString()}</div>
        </div>
      </div>

      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
            <tr>
              <th>VPA</th>
              <th>Payee name</th>
              {distinct ? <th className="num">Slips</th> : null}
              <th className="num">{distinct ? 'Total amount' : 'Amount'}</th>
              <th>Status</th>
              <th>{distinct ? 'First → last seen' : 'Source (where found)'}</th>
              <th>{distinct ? 'Sample slip' : 'QR uploaded'}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => {
              if (seen.has(r.key)) return null
              seen.add(r.key)
              return (
                <tr key={r.key}>
                  <td className="mono" style={{ fontWeight: r.known_mule ? 400 : 700 }}>{r.vpa}</td>
                  <td className="small">{r.payee_name ?? '—'}</td>
                  {distinct ? <td className="num"><b>{(r.slips ?? 0).toLocaleString()}</b></td> : null}
                  <td className="num">{(distinct ? r.total_inr : r.amount_inr) != null ? fmtINR((distinct ? r.total_inr : r.amount_inr) as number) : '—'}</td>
                  <td>
                    {r.known_mule
                      ? <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)' }}>known mule</span>
                      : <span className="ex-badge ex-badge-new">NEW</span>
                    }
                  </td>
                  {distinct
                    ? <td className="small dim">{r.first_seen ? fmtTime(r.first_seen) : '—'} → {r.last_seen ? fmtTime(r.last_seen) : '—'} <span className="ex-badge" title={r.origin === 'qr+deposit' ? 'Seen on collection QRs AND deposit slips' : r.origin === 'qr' ? 'Seen on collection QRs' : 'Seen on deposit slips (money paid to this VPA)'}>{r.origin ?? '—'}</span>{r.has_sender ? <span className="ex-badge ex-badge-new" title="This VPA also appears on the Paid-by side of some slip — mixed role, verify before action">also sender</span> : null}</td>
                    : <td className="small dim mono" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key}><span className="ex-badge" title={r.kind === 'deposit' ? 'Deposit slip — money paid to this VPA' : 'Collection QR image'}>{r.kind === 'deposit' ? 'slip' : 'QR'}</span> {r.key}</td>}
                  {distinct
                    ? <td className="small dim mono" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key}>{r.key.split('/').pop()}</td>
                    : <td className="small dim">{fmtTime(r.last_modified)}</td>}
                  <td><button className="btn" style={{ padding: '2px 8px', fontSize: 11 }} onClick={() => setSelected(r)}>view</button></td>
                </tr>
              )
            })}
            {items.length === 0 && (
              <tr><td colSpan={7} className="dp-empty"><div className="dp-empty-icon">💳</div>No decoded VPAs yet — enrichment runs on bankingqr/, qrcode/, deposit/ slips</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
        <button className="btn" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>← Prev</button>
        <button className="btn" disabled={page + 1 >= pageCount} onClick={() => setPage((p) => p + 1)}>Next →</button>
        <span className="small dim">Page {page + 1} of {pageCount} · showing {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}</span>
      </div>

      {selected && (
        <div className="modal-backdrop" onClick={() => setSelected(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
            <div className="modal-head">
              <div style={{ minWidth: 0 }}>
                <div className="mono small" style={{ fontWeight: 700 }}>{selected.vpa}</div>
                <div className="small dim">{selected.payee_name ?? '—'}</div>
              </div>
              <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                <a className="btn" href={objectUrl(selected.key, true)}>Download</a>
                <a className="btn" href={objectUrl(selected.key)} target="_blank" rel="noreferrer">Open raw ↗</a>
                <button className="btn btn-primary" onClick={() => setSelected(null)}>Close <span className="kbd" style={{ marginLeft: 4 }}>Esc</span></button>
              </div>
            </div>
            <div className="modal-body">
              {imgErr ? (
                <div style={{ textAlign: 'center', padding: '32px 16px' }}>
                  <div style={{ fontSize: 26, marginBottom: 8 }}>🖼️</div>
                  <div className="small" style={{ marginBottom: 4 }}>Couldn't load this QR image</div>
                  <div className="tiny dim" style={{ marginBottom: 16, lineHeight: 1.6 }}>
                    The evidence mirror may be momentarily unreachable — the app retries via the live bucket automatically.
                    Use Download (sha256-verified) or retry in a moment.
                  </div>
                  <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
                    <button className="btn" onClick={() => setImgErr(false)}>↻ Retry</button>
                    <a className="btn btn-primary" href={objectUrl(selected.key, true)}>Download</a>
                  </div>
                </div>
              ) : (
                <img
                  src={imgErr ? undefined : objectUrl(selected.key)}
                  alt={selected.key}
                  onError={() => setImgErr(true)}
                  style={{ maxWidth: '100%', maxHeight: '60vh', display: 'block', margin: '0 auto' }}
                />
              )}
              <div className="panel" style={{ margin: '12px 0 0', padding: 12 }}>
                {[
                  ['Source file', selected.key],
                  ['Source folder', folderOf(selected.key)],
                  ['Evidence type', selected.kind === 'deposit' ? 'Deposit slip (money paid to this VPA)' : 'Collection QR image'],
                  ['Uploaded', fmtTime(selected.last_modified)],
                  ['Amount', selected.amount_inr != null ? fmtINR(selected.amount_inr) : '—'],
                  ['Status', selected.known_mule ? 'known mule' : 'NEW — not in master'],
                ].map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '5px 0', borderBottom: '1px solid rgba(255,255,255,.06)' }}>
                    <span className="tiny dim" style={{ textTransform: 'uppercase', letterSpacing: .6, fontWeight: 600 }}>{k}</span>
                    <span className="tiny mono" style={{ textAlign: 'right', wordBreak: 'break-all', maxWidth: 380 }}>{v}</span>
                  </div>
                ))}
                <button className="btn" style={{ width: '100%', marginTop: 10 }} onClick={() => navigator.clipboard.writeText(selected.key)}>Copy source object key</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
