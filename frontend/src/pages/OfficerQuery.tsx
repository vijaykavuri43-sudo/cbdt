import { useState } from 'react'
import { api, exportMoneyFlowsUniqueUrl, exportMoneyFlowsUrl, objectUrl, saveCsvFromUrl } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl } from '../lib/explorers'
import { fmtINR, fmtTime, previewJoined, shortKey } from '../lib/format'

type Folder = 'all' | 'deposit' | 'withdrawal' | 'bankingqr' | 'other'
type RailType = 'upi' | 'bank_account' | 'crypto_wallet'
type View = 'slips' | 'unique'

interface SlipRow {
  source_key: string
  direction: string
  gambler: string
  amount_inr?: number | null
  utr: string
  rail_type: string
  rail_id: string
  account_holder: string
  ifsc: string
  network: string
  last_modified: string
  seen_utc?: string
  has_object: boolean
}

interface UniqueRow {
  rail_type: string
  rail_id: string
  account_holder: string
  ifsc: string
  network: string
  utrs?: string
  utr_count?: number
  slips: number
  image_hits?: number
  amount_inr?: number | null
  first_seen: string
  last_seen: string
  sample_key: string
  has_object: boolean
}

interface SlipResp {
  transactions: number
  sum_amount_inr: number
  distinct_rails: number
  rows_total: number
  items: SlipRow[]
}

interface UniqueResp {
  transactions: number
  sum_amount_inr: number
  distinct_rails: number
  rows_total: number
  image_hits?: number
  decoded_wallets?: number
  items: UniqueRow[]
}

const FOLDERS: { id: Folder; label: string; hint: string }[] = [
  { id: 'all', label: 'All', hint: 'Every folder in the bucket' },
  { id: 'deposit', label: 'Deposit', hint: 'deposit/ customer slips' },
  { id: 'withdrawal', label: 'Withdrawal', hint: 'withdrawal/ payout proofs' },
  { id: 'bankingqr', label: 'Banking QR', hint: 'bankingqr/ + qrcode/' },
  { id: 'other', label: 'Other folders', hint: 'chat, gateway, rest' },
]

const RAILS: { id: RailType; label: string }[] = [
  { id: 'upi', label: 'UPI / VPA' },
  { id: 'bank_account', label: 'Bank accounts' },
  { id: 'crypto_wallet', label: 'Crypto wallets' },
]

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10)
}

function defaultFrom(): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - 7)
  return isoDate(d)
}

function defaultTo(): string {
  return isoDate(new Date())
}

function railColLabel(rail: RailType): string {
  if (rail === 'upi') return 'UPI / VPA'
  if (rail === 'bank_account') return 'Account number'
  return 'Wallet address'
}

function railFileTag(rail: RailType): string {
  if (rail === 'bank_account') return 'bank'
  if (rail === 'crypto_wallet') return 'crypto'
  return 'upi'
}

function csvFilename(kind: View, next: { folder: Folder; rail_type: RailType; since: string; until: string }): string {
  return `${kind === 'unique' ? 'unique-addresses' : 'all-slips'}-${next.folder}-${railFileTag(next.rail_type)}-${next.since}-to-${next.until}.csv`
}

export function OfficerQuery() {
  const [fromDate, setFromDate] = useState(defaultFrom)
  const [toDate, setToDate] = useState(defaultTo)
  const [folder, setFolder] = useState<Folder>('all')
  const [railType, setRailType] = useState<RailType>('upi')
  const [view, setView] = useState<View>('unique')
  const [queryInput, setQueryInput] = useState('')
  const [applied, setApplied] = useState<{
    since: string
    until: string
    folder: Folder
    rail_type: RailType
    view: View
    q: string
  } | null>(null)
  const [slips, setSlips] = useState<SlipResp | null>(null)
  const [uniques, setUniques] = useState<UniqueResp | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [dlBusy, setDlBusy] = useState<View | null>(null)
  const [page, setPage] = useState(0)
  const limit = 200

  const paramsOf = (next: NonNullable<typeof applied>) => ({
    rail_type: next.rail_type,
    q: next.q,
    since: next.since,
    until: next.until,
    folder: next.folder === 'all' ? '' : next.folder,
    source: 'object',
  })

  async function run(next: NonNullable<typeof applied>, nextPage = 0) {
    setError('')
    setBusy(true)
    setApplied(next)
    try {
      if (next.view === 'unique') {
        const resp = await api<UniqueResp>(
          '/api/money-flows-unique',
          { ...paramsOf(next), limit, offset: nextPage * limit },
          60000,
        )
        setUniques(resp)
        setSlips(null)
      } else {
        const resp = await api<SlipResp>(
          '/api/money-flows',
          { ...paramsOf(next), direction: 'both', limit, offset: nextPage * limit },
          60000,
        )
        setSlips(resp)
        setUniques(null)
      }
      setPage(nextPage)
    } catch (e) {
      setSlips(null)
      setUniques(null)
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  function apply() {
    if (!fromDate || !toDate) {
      setError('Select both From and To dates.')
      return
    }
    if (fromDate > toDate) {
      setError('From date must be on or before To date.')
      return
    }
    void run({
      since: fromDate,
      until: toDate,
      folder,
      rail_type: railType,
      view,
      q: queryInput.trim(),
    })
  }

  function pickFolder(next: Folder) {
    setFolder(next)
    if (applied) void run({ ...applied, folder: next, since: fromDate, until: toDate, q: queryInput.trim() })
  }

  function pickRail(next: RailType) {
    setRailType(next)
    if (applied) void run({ ...applied, rail_type: next, since: fromDate, until: toDate, q: queryInput.trim() })
  }

  function pickView(next: View) {
    setView(next)
    if (applied) void run({ ...applied, view: next, since: fromDate, until: toDate, q: queryInput.trim() })
  }

  async function downloadCsv(kind: View) {
    if (!applied) return
    setError('')
    setDlBusy(kind)
    try {
      const url = kind === 'unique'
        ? exportMoneyFlowsUniqueUrl(paramsOf(applied))
        : exportMoneyFlowsUrl({ ...paramsOf(applied), direction: 'both' })
      await saveCsvFromUrl(url, csvFilename(kind, applied))
    } catch (e) {
      setError(`CSV download failed: ${String(e)}`)
    } finally {
      setDlBusy(null)
    }
  }

  function setPreset(days: number) {
    const to = defaultTo()
    const from = new Date()
    from.setUTCDate(from.getUTCDate() - (days - 1))
    setFromDate(isoDate(from))
    setToDate(to)
  }

  const data = view === 'unique' ? uniques : slips
  const total = data?.rows_total ?? 0
  const folderHint = FOLDERS.find((f) => f.id === (applied?.folder ?? folder))?.hint ?? ''

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">⬇</span>
          <div>
            <h1 className="pg-header-title">Date range</h1>
            <div className="pg-header-sub">
              Folder where the file lives → UPI / bank / wallet → unique addresses for notices, all slips for evidence
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button
            className="btn"
            disabled={!applied || !!dlBusy}
            onClick={() => void downloadCsv('unique')}
            title="Excel CSV: one row per UPI/account/wallet for the dates, folder and rail you applied"
          >
            {dlBusy === 'unique' ? 'Preparing CSV…' : '⬇ Unique CSV'}
          </button>
          <button
            className="btn"
            disabled={!applied || !!dlBusy}
            onClick={() => void downloadCsv('slips')}
            title="Excel CSV: every matching slip with UTR, object path and timestamps"
          >
            {dlBusy === 'slips' ? 'Preparing CSV…' : '⬇ All-slips CSV'}
          </button>
        </div>
      </div>

      <div className="st-panel oq-panel">
        <div className="oq-grid oq-grid-dates">
          <label className="oq-field">
            <span className="tiny dim">From (UTC)</span>
            <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} />
          </label>
          <label className="oq-field">
            <span className="tiny dim">To (UTC)</span>
            <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} />
          </label>
          <div className="oq-field oq-presets-field">
            <span className="tiny dim">Quick range</span>
            <div className="oq-presets">
              <button type="button" className="ex-chip" onClick={() => setPreset(1)}>Today</button>
              <button type="button" className="ex-chip" onClick={() => setPreset(7)}>7 days</button>
              <button type="button" className="ex-chip" onClick={() => setPreset(30)}>30 days</button>
            </div>
          </div>
        </div>

        <div className="oq-seg">
          <span className="tiny dim">Source folder</span>
          <div className="oq-tabs" role="tablist">
            {FOLDERS.map((f) => (
              <button
                key={f.id}
                type="button"
                role="tab"
                className={'oq-tab' + (folder === f.id ? ' oq-tab-on' : '')}
                title={f.hint}
                onClick={() => pickFolder(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        <div className="oq-seg">
          <span className="tiny dim">Address type</span>
          <div className="oq-tabs" role="tablist">
            {RAILS.map((r) => (
              <button
                key={r.id}
                type="button"
                role="tab"
                className={'oq-tab' + (railType === r.id ? ' oq-tab-on' : '')}
                onClick={() => pickRail(r.id)}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        <label className="oq-field" style={{ marginTop: 10 }}>
          <span className="tiny dim">Optional search</span>
          <input
            value={queryInput}
            onChange={(e) => setQueryInput(e.target.value)}
            placeholder="VPA, account, wallet, UTR, holder…"
            onKeyDown={(e) => { if (e.key === 'Enter') apply() }}
          />
        </label>

        <div className="oq-actions">
          <span className="tiny dim">{folderHint}</span>
          <button className="btn btn-primary" disabled={busy} onClick={apply}>
            {busy ? 'Applying…' : 'Apply'}
          </button>
          {applied && (
            <button
              className="btn"
              disabled={!!dlBusy || busy}
              onClick={() => void downloadCsv(view)}
              title="Excel CSV of every row matching this date range, folder and address type — not just this page"
            >
              {dlBusy === view ? 'Preparing CSV…' : `⬇ Download this result as CSV`}
            </button>
          )}
        </div>
      </div>

      {error && <div className="money-error">Query failed: {error}</div>}

      {!applied && !error && (
        <div className="dp-empty">
          <div className="dp-empty-icon">📅</div>
          Set dates, pick All or a source folder, then UPI / bank / wallet, then Apply.
        </div>
      )}

      {applied && data && (
        <>
          <div className="st-cards money-cards" style={{ gridTemplateColumns: 'repeat(3, minmax(140px, 1fr))' }}>
            <div className="st-card st-card-accent">
              <div className="st-card-label">{view === 'unique' ? railColLabel(applied.rail_type) : 'Matching slips'}</div>
              <div className="st-card-value accent">{total.toLocaleString()}</div>
              <div className="tiny dim">{applied.since} → {applied.until} UTC · {FOLDERS.find((f) => f.id === applied.folder)?.label}</div>
            </div>
            {applied.rail_type === 'crypto_wallet' && view === 'unique' ? (
              <>
                <div className="st-card">
                  <div className="st-card-label">Decoded from images</div>
                  <div className="st-card-value">{(uniques?.image_hits ?? 0).toLocaleString()}</div>
                  <div className="tiny dim">{(uniques?.decoded_wallets ?? 0).toLocaleString()} addresses · same census as Crypto wallets</div>
                </div>
                <div className="st-card">
                  <div className="st-card-label">OCR payment slips</div>
                  <div className="st-card-value">{(data.transactions ?? 0).toLocaleString()}</div>
                  <div className="tiny dim">Amounts on crypto slips are often INR-misread — use Every slip, not rupee totals</div>
                </div>
              </>
            ) : (
              <>
                <div className="st-card">
                  <div className="st-card-label">Slips in window</div>
                  <div className="st-card-value">{(data.transactions ?? 0).toLocaleString()}</div>
                </div>
                <div className="st-card st-card-money">
                  <div className="st-card-label">Amount linked</div>
                  <div className="st-card-value money">{fmtINR(data.sum_amount_inr)}</div>
                </div>
              </>
            )}
          </div>

          <div className="oq-tabs" style={{ marginBottom: 10 }} role="tablist">
            <button type="button" className={'oq-tab' + (view === 'unique' ? ' oq-tab-on' : '')} onClick={() => pickView('unique')}>
              Unique addresses
            </button>
            <button type="button" className={'oq-tab' + (view === 'slips' ? ' oq-tab-on' : '')} onClick={() => pickView('slips')}>
              Every slip
            </button>
          </div>

          <div className="oq-dl-bar">
            <div>
              <strong>Download what you are viewing</strong>
              <div className="tiny dim">
                {FOLDERS.find((f) => f.id === applied.folder)?.label} · {RAILS.find((r) => r.id === applied.rail_type)?.label} · {applied.since} → {applied.until} UTC
                {' · '}CSV has all {total.toLocaleString()} {view === 'unique' ? 'addresses' : 'slips'}, not only this page of {limit}
              </div>
            </div>
            <div className="oq-dl-btns">
              <button
                className="btn btn-primary"
                disabled={!!dlBusy || total === 0}
                onClick={() => void downloadCsv(view)}
              >
                {dlBusy === view
                  ? 'Preparing CSV…'
                  : `⬇ CSV · ${total.toLocaleString()} ${view === 'unique' ? 'addresses' : 'slips'}`}
              </button>
              <button
                className="btn"
                disabled={!!dlBusy}
                onClick={() => void downloadCsv(view === 'unique' ? 'slips' : 'unique')}
              >
                {dlBusy && dlBusy !== view
                  ? 'Preparing CSV…'
                  : view === 'unique' ? 'CSV · every matching slip' : 'CSV · unique addresses'}
              </button>
            </div>
          </div>

          <section className="money-section">
            <div className="money-section-head">
              <div>
                <strong>{view === 'unique' ? 'One row per address' : 'Every matching slip'}</strong>
                <div className="tiny dim">
                  {view === 'unique' && applied.rail_type === 'crypto_wallet'
                    ? 'Decoded QR/image wallets in this window (Crypto wallets tab) plus any OCR slip rails. Image hits ≠ rupee amounts.'
                    : view === 'unique'
                      ? 'Freeze / notice list. CSV includes UTRs when OCR captured them.'
                      : 'Evidence pack. CSV includes object path, UTR and timestamps.'}
                </div>
              </div>
            </div>
            <div className="dp-table-wrap">
              {view === 'unique' ? (
                <table className="dp-table">
                  <thead>
                    <tr>
                      <th>{railColLabel(applied.rail_type)}</th>
                      {applied.rail_type !== 'crypto_wallet' && <th>UTR</th>}
                      {applied.rail_type !== 'upi' && <th>{applied.rail_type === 'bank_account' ? 'Holder / IFSC' : 'Network'}</th>}
                      {applied.rail_type === 'crypto_wallet' && <th className="num">Image hits</th>}
                      <th className="num">{applied.rail_type === 'crypto_wallet' ? 'OCR slips' : 'Slips'}</th>
                      {applied.rail_type !== 'crypto_wallet' && <th className="num">Amount</th>}
                      <th>First seen</th>
                      <th>Last seen</th>
                      <th>Sample object</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(uniques?.items ?? []).map((r, i) => {
                      const utrs = previewJoined(r.utrs)
                      return (
                      <tr key={`${r.rail_type}:${r.rail_id}:${i}`}>
                        <td className="mono money-rail">
                          {r.rail_id}
                          {applied.rail_type === 'crypto_wallet' && r.rail_id && (
                            <div className="tiny">
                              <a href={arkhamAddressUrl(r.rail_id)} target="_blank" rel="noreferrer">Arkham ↗</a>
                              {' · '}
                              <a href={bestTronscanUrl(r.rail_id) ?? '#'} target="_blank" rel="noreferrer">Tronscan ↗</a>
                            </div>
                          )}
                        </td>
                        {applied.rail_type !== 'crypto_wallet' && (
                          <td className="mono small" title={utrs.title || undefined}>
                            {utrs.text || '—'}
                            {utrs.extra > 0 ? <span className="tiny dim"> +{utrs.extra}</span> : null}
                          </td>
                        )}
                        {applied.rail_type !== 'upi' && (
                          <td>
                            {applied.rail_type === 'crypto_wallet' ? (
                              <div className="tiny dim mono">{r.network || '—'}</div>
                            ) : (
                              <>
                                <div>{r.account_holder || '—'}</div>
                                <div className="tiny dim mono">{r.ifsc || r.network || ''}</div>
                              </>
                            )}
                          </td>
                        )}
                        {applied.rail_type === 'crypto_wallet' && (
                          <td className="num">{(r.image_hits ?? 0).toLocaleString()}</td>
                        )}
                        <td className="num">{r.slips.toLocaleString()}</td>
                        {applied.rail_type !== 'crypto_wallet' && (
                          <td className="num money">{r.amount_inr != null ? fmtINR(r.amount_inr) : '—'}</td>
                        )}
                        <td className="small dim">{r.first_seen ? fmtTime(r.first_seen) : '—'}</td>
                        <td className="small dim">{r.last_seen ? fmtTime(r.last_seen) : '—'}</td>
                        <td>
                          {r.has_object ? (
                            <a href={objectUrl(r.sample_key)} target="_blank" rel="noreferrer" title={r.sample_key}>
                              {shortKey(r.sample_key, 28)}
                            </a>
                          ) : <span className="tiny dim">—</span>}
                        </td>
                      </tr>
                      )
                    })}
                    {(uniques?.items ?? []).length === 0 && (
                      <tr><td colSpan={8} className="dim">No addresses matched these filters.</td></tr>
                    )}
                  </tbody>
                </table>
              ) : (
                <table className="dp-table">
                  <thead>
                    <tr>
                      <th>Bucket time</th>
                      <th>Indexed</th>
                      <th className="num">Amount</th>
                      <th>UTR</th>
                      <th>{railColLabel(applied.rail_type)}</th>
                      {applied.rail_type !== 'upi' && <th>{applied.rail_type === 'bank_account' ? 'Holder / IFSC' : 'Network'}</th>}
                      <th>Source object</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(slips?.items ?? []).map((r, i) => (
                      <tr key={`${r.source_key}:${r.rail_id}:${i}`}>
                        <td className="small dim" title={r.last_modified}>{r.last_modified ? fmtTime(r.last_modified) : '—'}</td>
                        <td className="small dim" title={r.seen_utc || ''}>{r.seen_utc ? fmtTime(r.seen_utc) : '—'}</td>
                        <td className="num money">{r.amount_inr != null ? fmtINR(r.amount_inr) : '—'}</td>
                        <td className="mono small">{r.utr || '—'}</td>
                        <td className="mono money-rail">{r.rail_id || 'unresolved'}</td>
                        {applied.rail_type !== 'upi' && (
                          <td>
                            <div>{r.account_holder || '—'}</div>
                            <div className="tiny dim mono">{r.ifsc || r.network || ''}</div>
                          </td>
                        )}
                        <td>
                          {r.has_object ? (
                            <a href={objectUrl(r.source_key)} target="_blank" rel="noreferrer" title={r.source_key}>
                              {shortKey(r.source_key, 28)}
                            </a>
                          ) : <span className="tiny dim">reference</span>}
                        </td>
                      </tr>
                    ))}
                    {(slips?.items ?? []).length === 0 && (
                      <tr><td colSpan={8} className="dim">No slips matched these filters.</td></tr>
                    )}
                  </tbody>
                </table>
              )}
            </div>
          </section>

          <div className="ex-pagination">
            <button className="ex-page-btn" disabled={page === 0 || busy || !!dlBusy} onClick={() => applied && run(applied, page - 1)}>← Prev</button>
            <span className="ex-page-info">page {page + 1} · {total.toLocaleString()} {view === 'unique' ? 'addresses' : 'slips'} · CSV downloads every matching row</span>
            <button className="ex-page-btn" disabled={busy || !!dlBusy || (page + 1) * limit >= total} onClick={() => applied && run(applied, page + 1)}>Next →</button>
          </div>
        </>
      )}
    </>
  )
}
