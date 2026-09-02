import { useEffect, useState } from 'react'
import { api, objectUrl, type ObjRow } from '../lib/api'

interface Props {
  obj: ObjRow
  onClose?: () => void
}

export function SheetViewer({ obj }: Props) {
  const [headers, setHeaders] = useState<string[]>([])
  const [rows, setRows] = useState<string[][]>([])
  const [, setTotalRows] = useState(0)
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(0)
  const pageSize = 50

  useEffect(() => {
    setLoading(true)
    setErr('')
    api<any>('/api/sheet', { key: obj.key, limit: 2000 })
      .then((d) => {
        setHeaders(d.headers ?? [])
        setRows(d.rows ?? [])
        setTotalRows(d.total_rows ?? 0)
        setTruncated(!!d.truncated)
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [obj.key])

  const filtered = q
    ? rows.filter((r) => r.join(' ').toLowerCase().includes(q.toLowerCase()))
    : rows
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const slice = filtered.slice(page * pageSize, (page + 1) * pageSize)

  useEffect(() => setPage(0), [q, obj.key])

  function exportCsv() {
    const csv = [headers, ...filtered].map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = obj.key.split('/').pop()!.replace(/\.(xlsx|xls)$/i, '.csv')
    a.click()
  }

  if (loading) return <div className="small dim">Loading sheet…</div>
  if (err) return <div className="small" style={{ color: 'var(--bad)' }}>{err}</div>

  return (
    <div>
      <div className="toolbar">
        <input type="text" placeholder="Search rows…" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }} />
        <span className="small dim">
          {filtered.length.toLocaleString()} / {rows.length.toLocaleString()} rows
          {truncated ? ' (truncated — download for full)' : ''}
        </span>
        <button className="btn" onClick={exportCsv}>⬇ Export filtered CSV</button>
        <a className="btn btn-primary" href={objectUrl(obj.key, true)} download>
          ⛨ Download original (+sha256)
        </a>
      </div>

      <div style={{ overflow: 'auto', maxHeight: 'min(72vh, calc(100vh - 220px))', maxWidth: '100%', border: '1px solid var(--border)', borderRadius: 8 }}>
        <table className="tbl" style={{ minWidth: 520, width: 'max-content' }}>
          <thead>
            <tr>
              {headers.map((h, i) => (
                <th key={i} className="mono" style={{ whiteSpace: 'nowrap', position: 'sticky', top: 0, background: 'var(--panel)' }}>
                  {h || `col_${i + 1}`}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.map((r, ri) => (
              <tr key={ri}>
                {r.map((c, ci) => (
                  <td key={ci} className="mono small" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {c}
                  </td>
                ))}
              </tr>
            ))}
            {slice.length === 0 && (
              <tr>
                <td colSpan={headers.length || 1} className="dim small">
                  No matching rows.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="toolbar" style={{ marginTop: 10 }}>
        <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>
          ← Prev
        </button>
        <span className="small dim">
          page {page + 1}/{pages}
        </span>
        <button className="btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>
          Next →
        </button>
      </div>
    </div>
  )
}
