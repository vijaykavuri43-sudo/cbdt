import { useCallback, useEffect, useState } from 'react'
import { api, post, type ObjRow } from '../lib/api'
import { fileColor, fileIcon, fmtBytes, fmtTime } from '../lib/format'
import { Preview } from '../components/Preview'

export function Search() {
  const [q, setQ] = useState('')
  const [prefix, setPrefix] = useState('')
  const [exts, setExts] = useState<Set<string>>(new Set())
  const [sinceDays, setSinceDays] = useState('')
  const [minSize, setMinSize] = useState('')
  const [items, setItems] = useState<ObjRow[]>([])
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [preview, setPreview] = useState<ObjRow | null>(null)
  const [busy, setBusy] = useState(false)

  const since = sinceDays ? new Date(Date.now() - Number(sinceDays) * 86400_000).toISOString().slice(0, 10) : ''

  const run = useCallback(() => {
    api<any>('/api/search', { q, prefix, ext: [...exts].join(','), since, min_size: minSize, limit: 1000 })
      .then((d) => setItems(d.items ?? []))
      .catch(() => {})
  }, [q, prefix, exts, since, minSize])

  useEffect(() => { const t = setTimeout(run, 350); return () => clearTimeout(t) }, [run])

  function toggleExt(e: string) { setExts((s) => { const n = new Set(s); if (n.has(e)) n.delete(e); else n.add(e); return n }) }

  async function downloadSel() {
    if (!sel.size) return; setBusy(true)
    try { await post('/api/download', { keys: [...sel], label: 'search' }); alert('Queued — see Evidence tab.'); setSel(new Set()) }
    finally { setBusy(false) }
  }

  const extChips: [string, string][] = [
    ['.jpg', '🖼 JPG'], ['.png', '🖼 PNG'], ['.wav', '🔊 WAV'],
    ['.mp4,.mov,.webm', '🎬 Video'], ['.xlsx,.xls,.csv', '📊 Ledgers'],
    ['.pdf', '📕 PDF'], ['.json', '📝 JSON'], ['.txt,.log', '📄 TXT'], ['.apk', '📲 APK'],
  ]
  const timeChips: [string, string][] = [['', 'Any'], ['1', '24h'], ['7', '7d'], ['30', '30d']]

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">⌕</span>
          <div>
            <h1 className="pg-header-title">Search All Data</h1>
            <div className="pg-header-sub">Full-manifest search across all indexed keys</div>
          </div>
        </div>
        {sel.size > 0 && (
          <div className="pg-header-right">
            <span className="ex-sel-count">{sel.size} selected</span>
            <button className="btn btn-primary" disabled={busy} onClick={downloadSel} style={{ fontSize: 11 }}>⛨ Download</button>
          </div>
        )}
      </div>

      {/* Search bar */}
      <div className="sr-bar">
        <input
          className="sr-input"
          type="text"
          autoFocus
          placeholder='e.g. pavan, utr, .apk, kyc, aadhaar…'
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <input
          type="text"
          placeholder="folder prefix"
          value={prefix}
          onChange={(e) => setPrefix(e.target.value)}
          style={{ width: 160, fontSize: 12, padding: '6px 10px' }}
          className="mono"
        />
        <select value={minSize} onChange={(e) => setMinSize(e.target.value)} style={{ fontSize: 11, padding: '5px 8px' }}>
          <option value="">Any size</option>
          <option value="102400">≥ 100 KB</option>
          <option value="1048576">≥ 1 MB</option>
          <option value="10485760">≥ 10 MB</option>
        </select>
      </div>

      {/* Filter chips */}
      <div className="sr-chips" style={{ marginBottom: 10 }}>
        {extChips.map(([v, label]) => (
          <button key={v} className={`ex-chip ${[...exts].some((x) => v.split(',').includes(x)) ? 'ex-chip-on' : ''}`} onClick={() => toggleExt(v)}>{label}</button>
        ))}
        <span style={{ width: 8 }} />
        {timeChips.map(([v, label]) => (
          <button key={v} className={`ex-chip ${sinceDays === v ? 'ex-chip-on' : ''}`} onClick={() => setSinceDays(v)}>{label}</button>
        ))}
      </div>

      {/* Results info */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <span className="small dim">{items.length.toLocaleString()} results (max 1000, newest first)</span>
      </div>

      {/* Results */}
      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
            <tr>
              <th style={{ width: 30 }}></th>
              <th>Key</th>
              <th className="num" style={{ width: 90 }}>Size</th>
              <th style={{ width: 110 }}>Modified</th>
              <th style={{ width: 160 }}>Info</th>
            </tr>
          </thead>
          <tbody>
            {items.map((o) => {
              const icon = fileIcon(o.key)
              const color = fileColor(o.key)
              return (
                <tr key={o.key}>
                  <td>
                    <button
                      className={`ex-row-check ${sel.has(o.key) ? 'ex-row-check-on' : ''}`}
                      onClick={() => setSel((s) => { const n = new Set(s); if (n.has(o.key)) n.delete(o.key); else n.add(o.key); return n })}
                    >
                      {sel.has(o.key) ? '✓' : ''}
                    </button>
                  </td>
                  <td className="mono small">
                    <span style={{ color, marginRight: 6 }}>{icon}</span>
                    <a onClick={() => setPreview(o)} style={{ cursor: 'pointer' }}>{o.key}</a>
                  </td>
                  <td className="num">{fmtBytes(o.size)}</td>
                  <td className="small dim">{fmtTime(o.last_modified).slice(0, 16)}</td>
                  <td>
                    {o.extract?.amount_inr != null && <span className="ex-badge ex-badge-money">₹{o.extract.amount_inr.toLocaleString('en-IN')}</span>}
                    {o.extract?.new_mule && <span className="ex-badge ex-badge-new">NEW VPA</span>}
                    {o.enriched === 1 && <span style={{ color: 'var(--accent2)', fontSize: 11 }}>✓</span>}
                  </td>
                </tr>
              )
            })}
            {items.length === 0 && (
              <tr><td colSpan={5} className="dp-empty"><div className="dp-empty-icon">🔍</div>{q ? 'No matches — try different keywords' : 'Type to search across all data'}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {preview && <Preview obj={preview} onClose={() => setPreview(null)} />}
    </>
  )
}
