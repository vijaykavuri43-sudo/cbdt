import { useCallback, useEffect, useState } from 'react'
import { api, objectUrl } from '../lib/api'
import { fmtINR, fmtTime } from '../lib/format'

interface UpiRow {
  key: string; last_modified: string; vpa?: string; payee_name?: string; amount_inr?: number | null; known_mule?: boolean; raw_text?: string | null
}

export function Upis() {
  const [data, setData] = useState<{ master_size: number; items: UpiRow[]; new_not_in_master: number } | null>(null)
  const [onlyNew, setOnlyNew] = useState(false)

  const refresh = useCallback(() => {
    api<any>('/api/upis', { limit: 500 }).then(setData).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])

  const items = (data?.items ?? []).filter((i) => (onlyNew ? !i.known_mule : true))
  const seen = new Set<string>()

  function exportTsv() {
    const header = 'VPA\tPayee Name\tAmount (INR)\tStatus\tQR File\n'
    const rows = items.map((r) => [r.vpa, r.payee_name ?? '', r.amount_inr ?? '', r.known_mule ? 'known' : 'NEW', r.key].join('\t')).join('\n')
    const blob = new Blob([header + rows], { type: 'text/tab-separated-values' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `upi-mules-${new Date().toISOString().slice(0, 10)}.tsv`; a.click()
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">@</span>
          <div>
            <h1 className="pg-header-title">UPI / Mule Accounts</h1>
            <div className="pg-header-sub">VPAs decoded from payment QRs · cross-checked against {(data?.master_size ?? 0).toLocaleString()} known mules</div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={exportTsv} style={{ fontSize: 11 }}>⬇ Export ({items.length})</button>
        </div>
      </div>

      <div className="lf-controls">
        <div className="lf-controls-left">
          <button className={`ex-toggle ${!onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(false)}>All · {data?.items.length ?? 0}</button>
          <button className={`ex-toggle ${onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(true)}>
            ⚠ NEW only · {data?.new_not_in_master ?? 0}
          </button>
        </div>
      </div>

      <div className="st-cards" style={{ marginBottom: 16 }}>
        <div className="st-card st-card-accent">
          <div className="st-card-label">Decoded QRs</div>
          <div className="st-card-value accent">{items.length.toLocaleString()}</div>
        </div>
        <div className="st-card st-card-warn">
          <div className="st-card-label">New — not in master</div>
          <div className="st-card-value warn">{(data?.new_not_in_master ?? 0).toLocaleString()}</div>
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
              <th className="num">Amount</th>
              <th>Status</th>
              <th>QR uploaded</th>
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
                  <td className="num">{r.amount_inr != null ? fmtINR(r.amount_inr) : '—'}</td>
                  <td>
                    {r.known_mule
                      ? <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)' }}>known mule</span>
                      : <span className="ex-badge ex-badge-new">NEW</span>
                    }
                  </td>
                  <td className="small dim">{fmtTime(r.last_modified)}</td>
                  <td><a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view QR</a></td>
                </tr>
              )
            })}
            {items.length === 0 && (
              <tr><td colSpan={6} className="dp-empty"><div className="dp-empty-icon">💳</div>No decoded VPAs yet — enrichment runs on bankingqr/, qrcode/, payment/</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}
