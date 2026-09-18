import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AuthError, api, objectUrl, post, type ObjRow } from '../lib/api'
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

// Display name: never let a stale/mismatched prefix eat the start of the
// filename (2026-09-05: root files rendered as "og"/"t_hour.log" under a
// currency/ breadcrumb after a failed navigation fetch left stale rows).
function displayName(key: string, prefix: string) {
  if (prefix && key.startsWith(prefix)) {
    const tail = key.slice(prefix.length)
    if (tail) return tail
  } else if (!prefix) {
    return key
  }
  return key.split('/').pop() || key
}

// Transient network failure? (connection reset, GCS wobble, api() timeout.
// Never auth/4xx: those must surface, not spin.) Eligible for one auto-retry.
function isTransientErr(e: any) {
  if (!e || e.name === 'AuthError') return false
  const s = String(e.message || e)
  if (/^4\d\d/.test(s) || /locked/i.test(s)) return false
  return e instanceof TypeError || /timed out/i.test(s) || /^5\d\d/.test(s)
}
function FolderTree({
  dirs,
  prefix,
  onNavigate,
  loading,
  error,
  onRetry,
}: {
  dirs: { seg: string; n: number; size: number }[]
  prefix: string
  onNavigate: (seg: string) => void
  loading: boolean
  error: string
  onRetry: () => void
}) {
  return (
    <div className="ex-sidebar">
      <div className="ex-sidebar-head">
        <span className="ex-sidebar-title">Folders</span>
        <span className="ex-sidebar-count">{dirs.length}</span>
      </div>
      <div className="ex-sidebar-list">
        {loading && dirs.length === 0 && !error && (
          <div className="ex-sidebar-empty">Loading…</div>
        )}
        {error && dirs.length === 0 && (
          <div className="ex-sidebar-empty">
            <span style={{ color: 'var(--bad)' }}>Folders failed to load</span>
            <div className="tiny dim" style={{ marginTop: 4, wordBreak: 'break-word' }}>{error}</div>
            <button className="btn tiny" style={{ marginTop: 8 }} onClick={onRetry}>Retry folders</button>
          </div>
        )}
        {!loading && !error && dirs.length === 0 && (
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
              title={`${prefix}${d.seg}/ — ${info.desc}`}
            >
              <span className="ex-folder-icon" style={{ color: info.color }}>{info.icon}</span>
              <div className="ex-folder-info">
                <span className="ex-folder-name">{d.seg}</span>
                <span className="ex-folder-path mono tiny dim">{prefix}{d.seg}/</span>
                <span className="ex-folder-meta">
                  {d.n.toLocaleString()} · {fmtBytes(d.size)}
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
  showPath,
}: {
  obj: ObjRow
  prefix: string
  onPreview: (o: ObjRow) => void
  selected: boolean
  onToggleSelect: (key: string) => void
  showPath?: boolean
}) {
  const img = isImg(obj.key)
  const ext = extOf(obj.key)
  const name = showPath ? obj.key : displayName(obj.key, prefix)
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
            src={objectUrl(obj.key, false, 320)}
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
          {name.length > 48 ? name.slice(0, 22) + '…' + name.slice(-22) : name}
        </div>
        {showPath && name !== obj.key && (
          <div className="ex-file-path mono tiny dim" title={obj.key}>{obj.key}</div>
        )}
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
export function Explorer({ prefixes, jumpTo }: { prefixes: string[]; jumpTo?: { p: string; t: number } | null }) {
  const [source] = useState<'manifest' | 'live'>('manifest')
  const [prefix, setPrefix] = useState('')
  const [q, setQ] = useState('')
  const [searchAll, setSearchAll] = useState(true)
  const [order] = useState<'key' | '-modified' | '-seen' | 'size'>('-seen')
  const [ext, setExt] = useState('')
  const [sinceDays, setSinceDays] = useState('')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [flat, setFlat] = useState(false)
  const [gallery, setGallery] = useState(true)
  const [page, setPage] = useState(0)
  const [limit] = useState(50)
  const [syncing, setSyncing] = useState(false)
  const [syncMsg, setSyncMsg] = useState('')
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
  const [treeErr, setTreeErr] = useState('')
  const [filterOpen, setFilterOpen] = useState(false)
  const reqIdRef = useRef(0)
  // rid already given its one automatic retry (transient-blip recovery).
  const retryRef = useRef(0)
  // Prefix the sidebar tree was last loaded for. Page turns / sort / search
  // must NOT refetch /api/tree (heavy GROUP BY over ~1.4M rows) — only an
  // actual folder change does (2026-09-05 efficiency pass).
  const treePrefixRef = useRef('@@none@@')
  const pageCache = useRef(new Map<string, { at: number; data: any }>())
  // Warm page N+1 in the background so Next feels instant; failures are
  // silent — the real navigation fetch remains the source of truth.
  const prefetchNext = useCallback((ck: string, d: any) => {
    try {
      const parts = ck.split('|')
      const total = d?.total ?? 0, lim = Number(parts[2]) || 50, pg = Number(parts[3]) || 0
      if ((pg + 1) * lim >= total) return
      const nk = `${parts[0]}|${parts[1]}|${parts[2]}|${pg + 1}|${parts[4]}|${parts[5]}|${parts[6]}|${parts[7]}|${parts[8]}`
      if (pageCache.current.has(nk)) return
      api<any>('/api/list', {
        prefix: parts[0], order: parts[1], limit: lim, offset: (pg + 1) * lim,
        q: parts[4], ext: parts[5], since: parts[6], until: parts[7], flat: parts[8] ? 1 : '',
      }).then((nd) => {
        pageCache.current.set(nk, { at: Date.now(), data: nd })
      }).catch(() => {})
    } catch { /* never break navigation */ }
  }, [])
  const contentRef = useRef<HTMLDivElement>(null)

  const since = fromDate || (sinceDays
    ? new Date(Date.now() - Number(sinceDays) * 86400_000).toISOString().slice(0, 10)
    : '')
  const until = toDate

  const [debouncedQ, setDebouncedQ] = useState(q)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 320)
    return () => clearTimeout(t)
  }, [q])

  const searching = debouncedQ.trim().length > 0
  const listPrefix = searching && searchAll ? '' : prefix
  const listFlat = searching && searchAll ? true : flat

  const liveCursor = cursorStack[cursorStack.length - 1] ?? ''

  const refresh = useCallback(() => {
    setLoading(true)
    setErrMsg('')
    setTreeErr('')
    const rid = ++reqIdRef.current
    // Absolute backstop: even if a fetch never settles, never skeleton forever.
    // (api() also aborts at 25s — this covers any other hang path.)
    const loadTimer = window.setTimeout(() => {
      if (rid !== reqIdRef.current) return
      setLoading(false); setInitialLoad(false)
      setErrMsg((prev) => prev || `Still loading after 20s (folder: /${prefix || ''}) — backend may be busy. Press Retry.`)
    }, 20000)
    // Transient network blips (connection reset, GCS wobble — seen 2026-09-05
    // across several endpoints) get ONE automatic retry so officers don't have
    // to. Auth/4xx errors never retry.
    const onListError = (e: any) => {
      if (rid !== reqIdRef.current) return
      console.error('[Explorer] list failed:', e)
      if (isTransientErr(e) && retryRef.current !== rid) {
        retryRef.current = rid
        window.clearTimeout(loadTimer)
        setErrMsg('Connection blip — retrying automatically…')
        window.setTimeout(() => { if (reqIdRef.current === rid) refresh() }, 1200)
        return
      }
      setErrMsg(String(e?.message || e))
    }
    if (source === 'live') {
      api<any>('/api/live/list', { prefix: listPrefix, flat: listFlat ? 1 : '', q: debouncedQ, limit, cursor: liveCursor })
        .then((d) => {
          if (rid !== reqIdRef.current) return
          setLiveItems(d.items ?? [])
          setNextCursor(d.has_more ? d.next_cursor : null)
        })
        .catch((e) => {
          if (rid !== reqIdRef.current) return
          console.error('[Explorer] live list failed:', e)
          onListError(e)
        })
        .finally(() => {
          if (rid !== reqIdRef.current) return
          window.clearTimeout(loadTimer)
          setLoading(false); setInitialLoad(false)
        })
      return
    }
    // Client page cache (60s): Back / re-click / Next-then-Prev answers instantly
  // without re-querying; server totals are cached 300s so this stays coherent.
  const ck = `${listPrefix}|${order}|${limit}|${page}|${debouncedQ}|${ext}|${since}|${until}|${listFlat ? 1 : ''}`
  // Folder tree is independent of the file-list page cache: a cache-hit
  // revisit must still (re)load the sidebar that resetFolderView cleared
  // (2026-09-05: Up-to-root showed files but an empty sidebar on cache hit).
  const fetchTree = () => {
    if (source !== 'manifest' || searching || listFlat) return
    if (treePrefixRef.current === prefix) return
    // Mark attempted up-front: page turns must not hammer a failing tree;
    // folder change or the Retry button resets the ref to force refetch.
    treePrefixRef.current = prefix
    api<any>('/api/tree', { prefix }).then((t) => {
      if (rid !== reqIdRef.current) return
      setDirs(t.dirs ?? [])
      setDirsPrefix(t.prefix || prefix)
    }).catch((e) => {
      // Folder-list failure is sidebar-local: it must never blank the file
      // grid's error state or masquerade as "no subfolders" (2026-09-05).
      console.error('[Explorer] tree failed:', e)
      if (rid === reqIdRef.current && !(e instanceof AuthError)) setTreeErr(String(e?.message || e))
    })
  }
  const cached = pageCache.current.get(ck)
  if (cached && Date.now() - cached.at < 60000) {
    setData(cached.data)
    window.clearTimeout(loadTimer)
    setLoading(false); setInitialLoad(false)
    prefetchNext(ck, cached.data)
    fetchTree()
    return
  }
  api<any>('/api/list', { prefix: listPrefix, q: debouncedQ, order, limit, offset: page * limit, ext, since, until, flat: listFlat ? 1 : '' })
      .then((d) => {
        if (rid !== reqIdRef.current) return
        pageCache.current.set(ck, { at: Date.now(), data: d })
        if (pageCache.current.size > 40) { const oldest = pageCache.current.keys().next().value; if (oldest !== undefined) pageCache.current.delete(oldest) }
        setData(d)
        prefetchNext(ck, d)
      })
      .catch((e) => {
        if (rid !== reqIdRef.current) return
        console.error('[Explorer] list failed:', e)
        onListError(e)
      })
      .finally(() => { if (rid === reqIdRef.current) { window.clearTimeout(loadTimer); setLoading(false); setInitialLoad(false) } })
    fetchTree()
    if (source === 'manifest' && (searching || listFlat)) {
      // Searching / recursing hides (not clears) the folder sidebar cards
      // via render guards; dirs stay so un-searching is instant.
    }
  }, [source, prefix, listPrefix, listFlat, searching, debouncedQ, order, limit, page, ext, since, until, liveCursor])

  useEffect(refresh, [refresh])
  useEffect(() => { setPage(0); setCursorStack([null]) }, [prefix, debouncedQ, order, ext, sinceDays, fromDate, toDate, flat, source, searchAll])
  useEffect(() => {
    if (jumpTo == null) return
    resetFolderView()
    setPrefix(normPrefix(jumpTo.p))
    setQ('')
    setSearchAll(false)
  }, [jumpTo])

  function goNext() {
    if (source === 'live') { setCursorStack((s) => [...s, nextCursor]); contentRef.current?.scrollTo({ top: 0 }) }
    else setPage(page + 1)
  }
  function goPrev() {
    if (source === 'live') { setCursorStack((s) => s.length > 1 ? s.slice(0, -1) : s); contentRef.current?.scrollTo({ top: 0 }) }
    else setPage(Math.max(0, page - 1))
  }

  const crumbs = useMemo(() => {
    // filter(Boolean): a '//' prefix must never render a ghost empty crumb
    // (2026-09-11: '/paymentgateway//csv-files/' showed a blank folder icon).
    const parts = prefix ? prefix.replace(/\/$/, '').split('/').filter(Boolean) : []
    return parts.map((p, i) => ({ name: p, path: parts.slice(0, i + 1).join('/') + '/' }))
  }, [prefix])

  function toggle(key: string) {
    setSel((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n })
  }

  function openDir(seg: string) {
    const s = seg.replace(/^\/+|\/+$/g, '')
    if (!s) return
    resetFolderView()
    // Strip the base's trailing slash too: it always ends with '/' below
    // root, so base + '/' produced 'paymentgateway//csv-files/' (2026-09-11:
    // every subfolder click below root matched zero keys + ghost crumb).
    const base = normPrefix(prefix).replace(/\/+$/, '')
    setPrefix((base ? base + '/' : '') + s + '/')
  }

  function goPrefix(p: string) {
    resetFolderView()
    setPrefix(normPrefix(p)); setQ('')
  }

  function tryJumpToPath(raw: string) {
    const t = raw.trim().replace(/^\/+/, '')
    if (!t) return false
    const folderish = t.endsWith('/')
      || prefixes.some((p) => p.replace(/\/$/, '') === t.replace(/\/$/, ''))
      || (!t.includes('.') && t.includes('/'))
    if (!folderish) return false
    const path = t.endsWith('/') ? t : t + '/'
    goPrefix(path)
    setQ('')
    return true
  }

  function submitSearch() {
    if (tryJumpToPath(q)) return
    setDebouncedQ(q.trim())
  }

  // Switching folders must never show the previous folder's files/folders
  // under the new breadcrumb (2026-09-05: stale root files + "No subfolders"
  // rendered under currency/ after a failed fetch). The page cache keeps
  // Back-navigation instant, so clearing here costs nothing.
  function resetFolderView() {
    setDirs([]); setDirsPrefix('')
    // Sentinel, never a real prefix (root itself is ''): guarantees the next
    // refresh refetches the folder tree — 2026-09-05: resetting to '' skipped
    // the root tree, leaving "No subfolders here" at bucket root.
    treePrefixRef.current = '@@none@@'
    setData({ total: 0, items: [] })
    setLiveItems([])
    setNextCursor(null)
    setSel(new Set())
    setErrMsg(''); setTreeErr('')
  }

  async function downloadSelected() {
    if (sel.size === 0) return
    setBusy(true)
    try {
      await post('/api/download', { keys: [...sel], label: `explorer:${prefix || 'root'}` })
      setSel(new Set())
      alert('Download queued. Files save on this host with sha256 logging.')
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
          const d = await api<{ items: ObjRow[] }>('/api/list', { prefix: listPrefix, q, ext, since, until, flat: 1, limit: 1000, offset: off })
          keys.push(...d.items.map((o) => o.key)); if (d.items.length < 1000) break
        }
      }
      keys = keys.slice(0, cap)
      if (keys.length) await post('/api/download', { keys, label: `folder:${prefix || 'root'}` })
      alert(`${keys.length} files queued for download on this host.`)
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
      {source === 'manifest' && !searching && (loading || dirs.length > 0 || treeErr) && (
        <FolderTree
          dirs={dirs}
          prefix={prefix}
          onNavigate={(seg) => { openDir(seg); setQ(''); setExt(''); setSinceDays(''); setFlat(false) }}
          loading={loading}
          error={treeErr}
          onRetry={() => { treePrefixRef.current = '@@none@@'; refresh() }}
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
                <button className="ex-crumb" onClick={() => goPrefix(c.path)} title={c.path}>
                  <span style={{ color: ci.color }}>{ci.icon}</span>
                  {c.name}
                </button>
              </span>
            )
          })}
          {prefix && (
            <span className="ex-crumb-path mono tiny dim" title="Exact folder path">{prefix ? `/${prefix}` : '/'}</span>
          )}
          {!prefix && <span className="ex-crumb-path mono tiny dim">/</span>}
        </div>
        {searching && (
          <div className="tiny dim" style={{ margin: '0 0 8px 2px' }}>
            Searching {searchAll ? 'all folders' : `in /${prefix || ''}`} for <code className="mono">{debouncedQ}</code>
            {since || until ? ` · uploaded ${since || '…'} → ${until || 'now'}` : ''} · {total.toLocaleString()} matches
          </div>
        )}

        {/* Toolbar — compact single row */}
        <div className="ex-toolbar">
          <div className="ex-toolbar-left">
            <div className="ex-search-wrap">
              <span className="ex-search-icon">⌕</span>
              <input
                type="text"
                placeholder="Search name or path…  ( / )"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submitSearch() } }}
                id="ex-search"
                className="ex-search"
              />
              {q && <button className="ex-search-clear" onClick={() => setQ('')}>×</button>}
            </div>
            <button className="btn" style={{ fontSize: 12 }} onClick={submitSearch} title="Search by object name or full path">Search</button>
            <button
              className={`ex-toggle ${filterOpen ? 'ex-toggle-on' : ''}`}
              onClick={() => setFilterOpen(!filterOpen)}
            >
              Filters
              {(ext || sinceDays || fromDate || toDate) && <span className="ex-filter-dot" />}
            </button>
            <button className={`ex-toggle ${gallery ? 'ex-toggle-on' : ''}`} onClick={() => setGallery(!gallery)} title="G to toggle">
              {gallery ? 'Grid' : 'List'}
            </button>
            <button className="ex-sync-btn" onClick={doSync} disabled={syncing} title="Refresh this folder">
              {syncing ? '⏳' : '↻'}
            </button>
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
                { v: '.wav,.mp3,.ogg,.amr,.m4a,.opus,.webm,.3gp', l: '🔊 Audio' },
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
              <span className="ex-filter-label">Uploaded</span>
              {[
                { v: '', l: 'Any' },
                { v: '1', l: '24h' },
                { v: '7', l: '7d' },
                { v: '30', l: '30d' },
              ].map(({ v, l }) => (
                <button
                  key={v}
                  className={`ex-chip ${v === sinceDays && !fromDate ? 'ex-chip-on' : (!v && !sinceDays && !fromDate && !toDate ? 'ex-chip-on' : '')}`}
                  onClick={() => {
                    setSinceDays(v)
                    if (!v) { setFromDate(''); setToDate('') }
                    else {
                      setFromDate(new Date(Date.now() - Number(v) * 86400_000).toISOString().slice(0, 10))
                      setToDate(new Date().toISOString().slice(0, 10))
                    }
                  }}
                >
                  {l}
                </button>
              ))}
              <label className="tiny dim" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                From
                <input type="date" value={fromDate} onChange={(e) => { setFromDate(e.target.value); setSinceDays('') }} style={{ fontSize: 12, padding: '4px 6px' }} />
              </label>
              <label className="tiny dim" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                To
                <input type="date" value={toDate} onChange={(e) => { setToDate(e.target.value); setSinceDays('') }} style={{ fontSize: 12, padding: '4px 6px' }} />
              </label>
            </div>
          </div>
        )}

        {/* Directory stats bar */}
        {source === 'manifest' && dirsPrefix === prefix && dirs.length > 0 && !flat && !searching && (
          <div className="ex-dir-stats">
            <span className="ex-dir-stats-icon" style={{ color: info.color }}>{info.icon}</span>
            <span>{dirs.length} folders</span>
            <span className="ex-dir-stats-sep">·</span>
            <span>{data.total.toLocaleString()} files here</span>
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
              {rows().map((o) => (
                <FileCard
                  key={o.key}
                  obj={o}
                  prefix={prefix}
                  onPreview={setPreview}
                  selected={sel.has(o.key)}
                  onToggleSelect={toggle}
                  showPath={searching || listFlat}
                />
              ))}

              {rows().length === 0 && !loading && (
                <div className="ex-empty">
                  <div className="ex-empty-icon">🔍</div>
                  <div className="ex-empty-title">Nothing found</div>
                  <div className="ex-empty-hint">
                    Folder: <code className="mono">/{prefix || ''}</code> · Try clearing filters or Search.
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
                    <th>Name / path</th>
                    <th className="num" style={{ width: 90 }}>Size</th>
                    <th style={{ width: 100 }}>Modified</th>
                    <th style={{ width: 160 }}>Info</th>
                  </tr>
                </thead>
                <tbody>
                  {rows().map((o) => {
                    const name = searching || listFlat ? o.key : displayName(o.key, prefix)
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
                          <a className="ex-row-name" onClick={() => setPreview(o)} title={o.key}>
                            {shortKey(name, 72)}
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
