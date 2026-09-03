import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, objectUrl, post, type ObjRow } from '../lib/api'
import { extOf, fileColor, fileIcon, fmtBytes, fmtTime, folderInfo, shortKey } from '../lib/format'
import { Preview } from '../components/Preview'

const IMG_EXTS = ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.jfif', '.heic', '.avif']

function isImg(key: string) {
  return IMG_EXTS.includes(extOf(key))
}

// Canonical folder prefix: never a leading slash (openDir from bucket root
// used to build '/banner/', which matches zero object keys — 2026-09-03).
function normPrefix(p: string) {
  return p.replace(/^\/+/, '')
}

// ---- Folder sidebar tree ----
function FolderTree({
  dirs,
  prefix,
  onNavigate,
  loading,
}: {
  dirs: { seg: string; n: number; size: number }[]
  prefix: string
  onNavigate: (seg: string) => void
  loading: boolean
}) {
  return (
    <div className="ex-sidebar">
      <div className="ex-sidebar-head">
        <span className="ex-sidebar-title">Folders</span>
        <span className="ex-sidebar-count">{dirs.length}</span>
      </div>
      <div className="ex-sidebar-list">
        {loading && dirs.length === 0 && (
          <div className="ex-sidebar-empty">Loading…</div>
        )}
        {!loading && dirs.length === 0 && (
          <div className="ex-sidebar-empty">
            No subfolders here
            <div className="tiny dim" style={{ marginTop: 4 }}>Files only in this directory</div>
          </div>
        )}
        {dirs.map((d) => {
          const info = folderInfo(prefix + d.seg)
          const child = prefix + d.seg + '/'
          const isActive = prefix === child || prefix.startsWith(child)
          return (
            <button
              key={d.seg}
              className={`ex-folder-item ${isActive ? 'ex-folder-item-active' : ''}`}
              onClick={() => onNavigate(d.seg)}
            >
              <span className="ex-folder-icon" style={{ color: info.color }}>{info.icon}</span>
              <div className="ex-folder-info">
                <span className="ex-folder-name">{d.seg}</span>
                <span className="ex-folder-meta">
                  {d.n.toLocaleString()} files · {fmtBytes(d.size)}
                </span>
              </div>
              <span className="ex-folder-arrow">›</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ---- File card for gallery ----
function FileCard({
  obj,
  prefix,
  onPreview,
  selected,
  onToggleSelect,
}: {
  obj: ObjRow
  prefix: string
  onPreview: (o: ObjRow) => void
  selected: boolean
  onToggleSelect: (key: string) => void
}) {
  const img = isImg(obj.key)
  const ext = extOf(obj.key)
  const name = obj.key.slice(prefix.length) || obj.key.split('/').pop() || obj.key
  const color = fileColor(obj.key)
  const icon = fileIcon(obj.key)
  const hasExtract = obj.extract?.amount_inr != null || obj.extract?.vpa || obj.extract?.new_mule
  const [imgErr, setImgErr] = useState(false)

  return (
    <div
      className={`ex-file-card ${selected ? 'ex-file-sel' : ''}`}
      onClick={() => onPreview(obj)}
    >
      <div className="ex-file-thumb">
        {img && !imgErr ? (
          <img
            className="ex-file-img"
            loading="lazy"
            decoding="async"
            src={objectUrl(obj.key)}
            alt=""
            onError={() => setImgErr(true)}
          />
        ) : (
          <div className="ex-file-placeholder" style={{ background: `${color}12` }}>
            <span className="ex-file-type-icon" style={{ color }}>{icon}</span>
            <span className="ex-file-ext mono" style={{ color }}>{ext || 'file'}</span>
          </div>
        )}
        {/* Extract badges overlay */}
        {hasExtract && (
          <div className="ex-file-badges">
            {obj.extract?.amount_inr != null && (
              <span className="ex-badge ex-badge-money">₹{obj.extract.amount_inr.toLocaleString('en-IN')}</span>
            )}
            {obj.extract?.vpa && (
              <span className="ex-badge">{obj.extract.vpa.slice(0, 18)}</span>
            )}
            {obj.extract?.new_mule && (
              <span className="ex-badge ex-badge-new">NEW</span>
            )}
          </div>
        )}
        {/* Selection checkbox */}
        <button
          className={`ex-file-check ${selected ? 'ex-file-check-on' : ''}`}
          onClick={(e) => { e.stopPropagation(); onToggleSelect(obj.key) }}
          title={selected ? 'Deselect' : 'Select for download'}
        >
          {selected ? '✓' : ''}
        </button>
      </div>
      <div className="ex-file-meta">
        <div className="ex-file-name" title={obj.key}>
          {name.length > 36 ? name.slice(0, 18) + '…' + name.slice(-16) : name}
        </div>
        <div className="ex-file-details">
          <span>{fmtBytes(obj.size)}</span>
          <span>{fmtTime(obj.last_modified).slice(0, 10)}</span>
          {obj.enriched === 1 && <span className="ex-file-enriched" title="Enriched">✓</span>}
        </div>
      </div>
    </div>
  )
}

// ---- Main Explorer ----
export function Explorer({ prefixes }: { prefixes: string[] }) {
  const [source, setSource] = useState<'manifest' | 'live'>('manifest')
  const [prefix, setPrefix] = useState('')
  const [q, setQ] = useState('')
  const [order, setOrder] = useState<'key' | '-modified' | 'size'>('-modified')
  const [ext, setExt] = useState('')
  const [sinceDays, setSinceDays] = useState('')
  const [flat, setFlat] = useState(false)
  const [gallery, setGallery] = useState(true)
  const [page, setPage] = useState(0)
  const [limit] = useState(50)
  const [syncing, setSyncing] = useState(false)
  const [syncMsg, setSyncMsg] = useState('')
  const [autoSync, setAutoSync] = useState('')
  const [data, setData] = useState<{ total: number; items: ObjRow[] }>({ total: 0, items: [] })
  const [dirs, setDirs] = useState<{ seg: string; n: number; size: number }[]>([])
  const [dirsPrefix, setDirsPrefix] = useState('')
  const [liveItems, setLiveItems] = useState<ObjRow[]>([])
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [preview, setPreview] = useState<ObjRow | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [initialLoad, setInitialLoad] = useState(true)
  const [errMsg, setErrMsg] = useState('')
  const [filterOpen, setFilterOpen] = useState(false)
  const reqIdRef = useRef(0)
  const contentRef = useRef<HTMLDivElement>(null)

  const since = sinceDays
    ? new Date(Date.now() - Number(sinceDays) * 86400_000).toISOString().slice(0, 10)
    : ''

  const [debouncedQ, setDebouncedQ] = useState(q)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 320)
    return () => clearTimeout(t)
  }, [q])

  const liveCursor = cursorStack[cursorStack.length - 1] ?? ''

  const refresh = useCallback(() => {
    setLoading(true)
    setErrMsg('')
    const rid = ++reqIdRef.current
    // Absolute backstop: even if a fetch never settles, never skeleton forever.
    // (api() also aborts at 25s — this covers any other hang path.)
    const loadTimer = window.setTimeout(() => {
      if (rid !== reqIdRef.current) return
      setLoading(false); setInitialLoad(false)
      setErrMsg((prev) => prev || `Still loading after 20s (folder: /${prefix || ''}) — backend may be busy. Press Retry.`)
    }, 20000)
    if (source === 'live') {
      api<any>('/api/live/list', { prefix, flat: flat ? 1 : '', q: debouncedQ, limit, cursor: liveCursor })
        .then((d) => {
          if (rid !== reqIdRef.current) return
          setLiveItems(d.items ?? [])
          setNextCursor(d.has_more ? d.next_cursor : null)
        })
        .catch((e) => {
          if (rid !== reqIdRef.current) return
          console.error('[Explorer] live list failed:', e)
          setErrMsg(String(e))
        })
        .finally(() => {
          if (rid !== reqIdRef.current) return
          window.clearTimeout(loadTimer)
          setLoading(false); setInitialLoad(false)
        })
      return
    }
    api<any>('/api/list', { prefix, q: debouncedQ, order, limit, offset: page * limit, ext, since, flat: flat ? 1 : '' })
      .then((d) => { if (rid === reqIdRef.current) setData(d) })
      .catch((e) => {
        if (rid !== reqIdRef.current) return
        console.error('[Explorer] list failed:', e)
        setErrMsg(String(e))
      })
      .finally(() => { if (rid === reqIdRef.current) { window.clearTimeout(loadTimer); setLoading(false); setInitialLoad(false) } })
    if (source === 'manifest' && !debouncedQ && !flat) {
      api<any>('/api/tree', { prefix }).then((t) => {
        if (rid !== reqIdRef.current) return
        setDirs(t.dirs ?? [])
        setDirsPrefix(t.prefix || prefix)
      }).catch((e) => { console.error('[Explorer] tree failed:', e); if (rid === reqIdRef.current) setErrMsg((prev) => prev || String(e)) })
    } else {
      setDirs([])
      setDirsPrefix('')
    }
  }, [source, prefix, debouncedQ, order, limit, page, ext, since, flat, liveCursor])

  useEffect(refresh, [refresh])
  useEffect(() => { setPage(0); setCursorStack([null]) }, [prefix, debouncedQ, order, ext, sinceDays, flat, source])

  function goNext() {
    if (source === 'live') { setCursorStack((s) => [...s, nextCursor]); contentRef.current?.scrollTo({ top: 0 }) }
    else setPage(page + 1)
  }
  function goPrev() {
    if (source === 'live') { setCursorStack((s) => s.length > 1 ? s.slice(0, -1) : s); contentRef.current?.scrollTo({ top: 0 }) }
    else setPage(Math.max(0, page - 1))
  }

  const crumbs = useMemo(() => {
    const parts = prefix ? prefix.replace(/\/$/, '').split('/') : []
    return parts.map((p, i) => ({ name: p, path: parts.slice(0, i + 1).join('/') + '/' }))
  }, [prefix])

  function toggle(key: string) {
    setSel((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n })
  }

  function openDir(seg: string) {
    const s = seg.replace(/^\/+|\/+$/g, '')
    if (!s) return
    setDirs([]); setDirsPrefix('')
    const base = normPrefix(prefix)
    setPrefix((base ? base + '/' : '') + s + '/')
  }

  function goPrefix(p: string) {
    setDirs([]); setDirsPrefix('')
    setPrefix(normPrefix(p)); setQ('')
  }

  async function downloadSelected() {
    if (sel.size === 0) return
    setBusy(true)
    try {
      await post('/api/download', { keys: [...sel], label: `explorer:${prefix || 'root'}` })
      setSel(new Set())
      alert('Download job queued — see Evidence tab.')
    } finally { setBusy(false) }
  }

  async function doSync() {
    setSyncing(true); setSyncMsg('')
    try {
      const d = await post<any>('/api/sync?prefix=' + encodeURIComponent(prefix))
      const n = d.new_objects ?? Object.values(d.new_by_prefix ?? {}).reduce((a: number, v: any) => a + v, 0) + (d.new_root_files ?? 0)
      setSyncMsg(n ? `+${n} new` : 'Up to date')
      refresh()
      setTimeout(() => setSyncMsg(''), 4000)
    } catch (e) { setSyncMsg(String(e)) } finally { setSyncing(false) }
  }

  useEffect(() => {
    if (!autoSync) return
    const id = setInterval(doSync, Number(autoSync) * 1000)
    return () => clearInterval(id)
  }, [autoSync, prefix])

  function selectPage() {
    setSel((s) => { const n = new Set(s); for (const o of rows()) if (n.size < 5000) n.add(o.key); return n })
  }

  async function downloadFolder() {
    const cap = Math.min(data.total || limit, 5000)
    if (!confirm(`Queue up to ${cap.toLocaleString()} objects under "${prefix || 'root'}"?`)) return
    setBusy(true)
    try {
      let keys: string[] = []
      if (source === 'live') {
        let cur: string | null = null
        for (let i = 0; i < 5; i++) {
          const d: any = await api<any>('/api/live/list', { prefix, flat: 1, q, limit: 1000, cursor: cur ?? '' })
          keys.push(...(d.items as ObjRow[]).map((o) => o.key))
          if (!d.has_more) break; cur = d.next_cursor; if (keys.length >= cap) break
        }
      } else {
        for (let off = 0; off < cap; off += 1000) {
          const d = await api<{ items: ObjRow[] }>('/api/list', { prefix, q, ext, since, flat: 1, limit: 1000, offset: off })
          keys.push(...d.items.map((o) => o.key)); if (d.items.length < 1000) break
        }
      }
      keys = keys.slice(0, cap)
      if (keys.length) await post('/api/download', { keys, label: `folder:${prefix || 'root'}` })
      alert(`${keys.length} keys queued — see Evidence tab.`)
    } finally { setBusy(false) }
  }

  function rows(): ObjRow[] { return source === 'live' ? liveItems : data.items }
  const total = source === 'live'
    ? liveItems.length + (nextCursor ? limit * (cursorStack.length - 1) + 1 : 0)
    : data.total
  const pages = Math.ceil(data.total / limit)

  // keyboard shortcuts
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (e.key === '/') { e.preventDefault(); document.getElementById('ex-search')?.focus() }
      else if (e.key === 'g' || e.key === 'G') setGallery((v) => !v)
      else if (e.key === 'ArrowLeft') goPrev()
      else if (e.key === 'ArrowRight') goNext()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [page, pages, source, cursorStack, nextCursor])

  const dirTotalSize = dirs.reduce((a, d) => a + d.size, 0)
  const info = folderInfo(prefix)

  return (
    <div className="ex-root">
      {/* ---- Left sidebar: folder tree ---- */}
      {source === 'manifest' && (
        <FolderTree
          dirs={dirs}
          prefix={prefix}
          onNavigate={(seg) => { openDir(seg); setQ(''); setExt(''); setSinceDays(''); setFlat(false) }}
          loading={loading}
        />
      )}

      {/* ---- Right content area ---- */}
      <div className="ex-content">
        {/* Breadcrumbs */}
        <div className="ex-breadcrumbs">
          <button className="ex-crumb ex-crumb-root" onClick={() => goPrefix('')}>
            <span style={{ color: info.color }}>{info.icon}</span>
            bucket root
          </button>
          {crumbs.map((c) => {
            const ci = folderInfo(c.path)
            return (
              <span key={c.path} style={{ display: 'contents' }}>
                <span className="ex-crumb-sep">/</span>
                <button className="ex-crumb" onClick={() => goPrefix(c.path)}>
                  <span style={{ color: ci.color }}>{ci.icon}</span>
                  {c.name}
                </button>
              </span>
            )
          })}
        </div>

        {/* Toolbar — compact single row */}
        <div className="ex-toolbar">
          <div className="ex-toolbar-left">
            <button
              className={`ex-toggle ${source === 'live' ? 'ex-toggle-on' : ''}`}
              title="Query the GCS evidence mirror directly (ground truth)"
              onClick={() => setSource(source === 'live' ? 'manifest' : 'live')}
            >
              {source === 'live' ? '● LIVE' : '○ Manifest'}
            </button>

            <div className="ex-search-wrap">
              <span className="ex-search-icon">⌕</span>
              <input
                type="text"
                placeholder='Search…  ( / )'
                value={q}
                onChange={(e) => setQ(e.target.value)}
                id="ex-search"
                className="ex-search"
              />
              {q && <button className="ex-search-clear" onClick={() => setQ('')}>×</button>}
            </div>

            <button
              className={`ex-toggle ${filterOpen ? 'ex-toggle-on' : ''}`}
              onClick={() => setFilterOpen(!filterOpen)}
            >
              ☰ Filters
              {(ext || sinceDays) && <span className="ex-filter-dot" />}
            </button>
          </div>

          <div className="ex-toolbar-right">
            <select value={order} onChange={(e) => setOrder(e.target.value as any)} disabled={source === 'live'} className="ex-select">
              <option value="-modified">Newest</option>
              <option value="key">A→Z</option>
              <option value="size">Largest</option>
            </select>

            <select value="" onChange={(e) => e.target.value && goPrefix(e.target.value)} className="ex-select">
              <option value="">Jump to…</option>
              {prefixes.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>

            <button className={`ex-toggle ${flat ? 'ex-toggle-on' : ''}`} onClick={() => setFlat(!flat)} title="Include descendants">
              {flat ? '↧ Recurse' : '↦ Direct'}
            </button>
            <button className={`ex-toggle ${gallery ? 'ex-toggle-on' : ''}`} onClick={() => setGallery(!gallery)} title="G to toggle">
              {gallery ? '▦ Grid' : '▤ List'}
            </button>

            <div className="ex-toolbar-divider" />

            <button className="ex-sync-btn" onClick={doSync} disabled={syncing}>
              {syncing ? '⏳' : '↻'}
            </button>
            <select value={autoSync} onChange={(e) => setAutoSync(e.target.value)} className="ex-select ex-select-sm">
              <option value="">Manual</option>
              <option value="30">30s</option>
              <option value="60">60s</option>
              <option value="300">5min</option>
            </select>
            {syncMsg && <span className="ex-sync-msg">{syncMsg}</span>}
          </div>
        </div>

        {/* Expandable filter row */}
        {filterOpen && (
          <div className="ex-filter-row">
            <div className="ex-filter-group">
              <span className="ex-filter-label">Type</span>
              {[
                { v: '', l: 'All' },
                { v: '.jpg,.jpeg,.png,.webp,.heic', l: '🖼 Images' },
                { v: '.wav,.mp3,.ogg,.amr', l: '🔊 Audio' },
                { v: '.mp4,.mov,.avi,.webm', l: '🎬 Video' },
                { v: '.xlsx,.xls,.csv', l: '📊 Ledgers' },
                { v: '.pdf,.doc,.docx', l: '📄 PDF/Docs' },
                { v: '.json,.txt,.log,.xml,.gz', l: '📝 Data' },
                { v: '.apk', l: '📲 APK' },
              ].map(({ v, l }) => (
                <button key={v} className={`ex-chip ${ext === v ? 'ex-chip-on' : ''}`} onClick={() => setExt(v)}>
                  {l}
                </button>
              ))}
            </div>
            <div className="ex-filter-group">
              <span className="ex-filter-label">Age</span>
              {[
                { v: '', l: 'Any' },
                { v: '1', l: '24h' },
                { v: '7', l: '7d' },
                { v: '30', l: '30d' },
              ].map(({ v, l }) => (
                <button key={v} className={`ex-chip ${sinceDays === v ? 'ex-chip-on' : ''}`} onClick={() => setSinceDays(v)}>
                  {l}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Directory stats bar */}
        {source === 'manifest' && dirsPrefix === prefix && dirs.length > 0 && !flat && !debouncedQ && (
          <div className="ex-dir-stats">
            <span className="ex-dir-stats-icon" style={{ color: info.color }}>{info.icon}</span>
            <span className="ex-dir-stats-desc">{info.desc}</span>
            <span className="ex-dir-stats-sep">·</span>
            <span>{dirs.length} subfolders</span>
            <span className="ex-dir-stats-sep">·</span>
            <span>{data.total.toLocaleString()} objects in this folder</span>
            <span className="ex-dir-stats-sep">·</span>
            <span>{fmtBytes(dirTotalSize)}</span>
          </div>
        )}

        {/* Action bar */}
        <div className="ex-actions">
          <span className="ex-actions-info">
            {source === 'manifest'
              ? `${total.toLocaleString()} total · ${page * limit + 1}–${Math.min((page + 1) * limit, total)}`
              : `${rows().length} shown · batch ${cursorStack.length}`}
          </span>
          <span style={{ flex: 1 }} />
          {sel.size > 0 && <span className="ex-sel-count">{sel.size} selected</span>}
          <button className="ex-action-btn" onClick={selectPage}>Select page</button>
          <button className="ex-action-btn ex-action-primary" disabled={sel.size === 0 || busy} onClick={downloadSelected}>
            ⛨ Download ({sel.size || 0})
          </button>
          <button className="ex-action-btn" disabled={total === 0 || busy} onClick={downloadFolder}>
            ⛨ Folder ({Math.min(total, 5000)})
          </button>
        </div>

        {errMsg && (
          <div style={{ marginBottom: 10, padding: '8px 12px', border: '1px solid var(--bad)', background: 'rgba(248,113,113,.08)', borderRadius: 8, color: 'var(--bad)', fontSize: 12 }}>
            Load failed: {errMsg} <button className="btn" style={{ marginLeft: 8, padding: '2px 8px' }} onClick={refresh}>Retry</button>
          </div>
        )}
        {loading && !initialLoad && (
          <div style={{ height: 2, background: 'var(--accent)', opacity: .6, marginBottom: 8, borderRadius: 99, animation: 'shimmer 1s infinite' }} />
        )}
        {/* Content */}
        <div className="ex-main" ref={contentRef}>
          {loading ? (
            /* Skeleton loading */
            <div className="ex-grid">
              {Array.from({ length: 12 }).map((_, i) => (
                <div key={i} className="ex-file-card ex-skeleton">
                  <div className="ex-skel-thumb" />
                  <div className="ex-skel-line" style={{ width: '70%' }} />
                  <div className="ex-skel-line" style={{ width: '45%' }} />
                </div>
              ))}
            </div>
          ) : gallery ? (
            /* ---- GALLERY VIEW ---- */
            <div className="ex-grid">
              {/* Folder cards first */}
              {source === 'manifest' && dirsPrefix === prefix && !flat && !debouncedQ && dirs.map((d) => {
                const di = folderInfo(prefix + d.seg)
                return (
                  <div
                    key={'dir-' + d.seg}
                    className="ex-file-card ex-folder-card"
                    onClick={() => openDir(d.seg)}
                  >
                    <div className="ex-folder-card-icon" style={{ background: `${di.color}18` }}>
                      <span style={{ fontSize: 28, color: di.color }}>{di.icon}</span>
                    </div>
                    <div className="ex-folder-card-info">
                      <div className="ex-folder-card-name">{d.seg}</div>
                      <div className="ex-folder-card-meta">
                        <span>{d.n.toLocaleString()} files</span>
                        <span>·</span>
                        <span>{fmtBytes(d.size)}</span>
                      </div>
                      <div className="ex-folder-card-desc dim">{di.desc}</div>
                    </div>
                    <span className="ex-folder-card-arrow">›</span>
                  </div>
                )
              })}

              {/* Files */}
              {rows().map((o) => (
                <FileCard
                  key={o.key}
                  obj={o}
                  prefix={prefix}
              onPreview={setPreview}
                  selected={sel.has(o.key)}
                  onToggleSelect={toggle}
                />
              ))}

              {rows().length === 0 && dirs.length === 0 && !loading && (
                <div className="ex-empty">
                  <div className="ex-empty-icon">🔍</div>
                  <div className="ex-empty-title">Nothing found</div>
                  <div className="ex-empty-hint">
                    Folder: <code className="mono">/{prefix || ''}</code> · Try clearing filters, toggling Recursive, or hitting Sync.
                  </div>
                </div>
              )}
            </div>
          ) : (
            /* ---- TABLE VIEW ---- */
            <div className="ex-table-wrap">
              <table className="tbl ex-table">
                <thead>
                  <tr>
                    <th style={{ width: 30 }}></th>
                    <th>Name</th>
                    <th className="num" style={{ width: 90 }}>Size</th>
                    <th style={{ width: 100 }}>Modified</th>
                    <th style={{ width: 160 }}>Info</th>
                  </tr>
                </thead>
                <tbody>
                  {/* Folder rows first */}
                  {source === 'manifest' && dirsPrefix === prefix && !flat && !debouncedQ && dirs.map((d) => {
                    const di = folderInfo(prefix + d.seg)
                    return (
                      <tr key={'dir-' + d.seg} className="ex-table-folder" onClick={() => openDir(d.seg)}>
                        <td><span style={{ color: di.color }}>{di.icon}</span></td>
                        <td className="mono">
                          <span className="ex-table-folder-name" style={{ color: di.color }}>{d.seg}/</span>
                          <span className="dim tiny" style={{ marginLeft: 8 }}>{di.desc}</span>
                        </td>
                        <td className="num dim">{fmtBytes(d.size)}</td>
                        <td colSpan={2} className="dim small">{d.n.toLocaleString()} objects</td>
                      </tr>
                    )
                  })}
                  {/* File rows */}
                  {rows().map((o) => {
                    const name = o.key.slice(prefix.length)
                    const icon = fileIcon(o.key)
                    return (
                      <tr key={o.key}>
                        <td>
                          <button
                            className={`ex-row-check ${sel.has(o.key) ? 'ex-row-check-on' : ''}`}
                            onClick={() => toggle(o.key)}
                          >
                            {sel.has(o.key) ? '✓' : ''}
                          </button>
                        </td>
                        <td className="mono">
                          <span className="ex-row-icon">{icon}</span>
                          <a className="ex-row-name" onClick={() => setPreview(o)}>
                            {shortKey(name, 56)}
                          </a>
                        </td>
                        <td className="num">{fmtBytes(o.size)}</td>
                        <td className="small dim">{fmtTime(o.last_modified).slice(0, 16)}</td>
                        <td>
                          {o.extract?.amount_inr != null && (
                            <span className="ex-badge ex-badge-money">₹{o.extract.amount_inr.toLocaleString('en-IN')}</span>
                          )}
                          {o.extract?.new_mule && <span className="ex-badge ex-badge-new">NEW VPA</span>}
                          {o.extract?.utr && <span className="ex-badge">UTR</span>}
                          {o.enriched === 1 && <span className="ex-file-enriched" title="Enriched">✓</span>}
                        </td>
                      </tr>
                    )
                  })}
                  {dirs.length === 0 && rows().length === 0 && (
                    <tr>
                      <td colSpan={5} className="ex-empty-row">
                        <div className="ex-empty-icon">🔍</div>
                        <div className="ex-empty-title">Nothing found</div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Pagination */}
        <div className="ex-pagination">
          <button className="ex-page-btn" disabled={source === 'live' ? cursorStack.length <= 1 : page === 0} onClick={goPrev}>
            ← Prev
          </button>
          <span className="ex-page-info">
            {source === 'live'
              ? `batch ${cursorStack.length}`
              : `page ${page + 1} / ${pages || 1}`}
          </span>
          <button className="ex-page-btn" disabled={source === 'live' ? !nextCursor : page + 1 >= pages} onClick={goNext}>
            Next →
          </button>
        </div>
      </div>

      {preview && <Preview obj={preview} onClose={() => setPreview(null)} />}
    </div>
  )
}
