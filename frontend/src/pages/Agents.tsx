import { useCallback, useEffect, useState } from 'react'
import { api, tabHidden } from '../lib/api'
import { fmtINR } from '../lib/format'

interface Desk {
  operator: string; rows: number; clients: number; customers: number
  deposit_amount_inr: number | null; test_rows: number | null; first_seen?: string; last_seen?: string
}
interface Counted { name: string; agents: number }
interface LoginRow {
  username: string; mobile?: string; brand?: string; upline_manager?: string
  logins?: number; ip?: string; city?: string; region?: string; isp?: string
}
interface StaffBettor {
  username: string; logins?: number; ips?: string[]; city?: string; attribution?: string; note?: string
}
interface Neg { id: string; claim: string; result: string; evidence: string }
interface Corpus { pattern: string; files?: number; indexed?: number; size_mb?: number; priority?: string }
interface AgentRow {
  username: string; mobiles?: string[]; origins?: string[]
  upline_managers?: string[]; child_accounts?: number; export_files?: number
}
interface AgentsData {
  summary: {
    operator_consoles: number; agents: number; agents_with_mobile: number
    distinct_mobiles: number; mobiles_shared_by_multiple_agents: number
    upline_managers: number; brand_origins: number; agents_with_login_evidence: number
  }
  operator_desks: Desk[]
  top_upline_managers: Counted[]
  top_brand_origins: Counted[]
  login_evidence: LoginRow[]
  staff_like_bettors: StaffBettor[]
  negative_results: Neg[]
  unmined_corpora: Corpus[]
  items: AgentRow[]
  total: number
  roster_source?: string
  roster_collected_utc?: string
  roster_note?: string
  scope_note?: string
  guard_rail?: string
}

type View = 'desks' | 'agents' | 'managers' | 'brands' | 'login' | 'corpora'
const VIEWS: { id: View; label: string }[] = [
  { id: 'desks', label: 'Operator consoles' },
  { id: 'agents', label: 'Agents' },
  { id: 'managers', label: 'Upline managers' },
  { id: 'brands', label: 'Brand origins' },
  { id: 'login', label: 'Login evidence' },
  { id: 'corpora', label: 'Evidence state' },
]

export function Agents() {
  const PAGE = 200
  const [data, setData] = useState<AgentsData | null>(null)
  const [view, setView] = useState<View>('desks')
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    // Never swallow errors into an empty screen (2026-09-16 lesson): the
    // roster is an artifact read, so this is fast, but a missing artifact
    // must be visible rather than rendering "0 agents".
    setError('')
    setLoading(true)
    api<AgentsData>('/api/agents', { limit: PAGE, offset: page * PAGE }, 30000)
      .then((d) => { setData(d); setLoading(false) })
      .catch((e) => { setError(String(e)); setLoading(false) })
  }, [page])

  useEffect(refresh, [refresh])
  useEffect(() => { setPage(0) }, [view])
  useEffect(() => {
    const t = setInterval(() => { if (!tabHidden()) refresh() }, 120000)
    return () => clearInterval(t)
  }, [refresh])

  const s = data?.summary
  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE))

  return (
    <div className="page">
      <h2>Agents &amp; operator identity</h2>
      <p className="tiny dim">{data?.scope_note}</p>

      {error && (
        <div className="money-error">
          Could not load agent data: {error}
          <button className="btn" style={{ marginLeft: 8 }} onClick={refresh}>Retry</button>
        </div>
      )}

      <div className="st-cards money-cards">
        <div className="st-card st-card-accent">
          <div className="st-card-label">Operator consoles</div>
          <div className="st-card-value accent">{(s?.operator_consoles ?? 0).toLocaleString()}</div>
          <div className="tiny dim">live from gateway actionby</div>
        </div>
        <div className="st-card st-card-money">
          <div className="st-card-label">Agent accounts</div>
          <div className="st-card-value money">{(s?.agents ?? 0).toLocaleString()}</div>
          <div className="tiny dim">{(s?.agents_with_mobile ?? 0).toLocaleString()} with mobile</div>
        </div>
        <div className="st-card st-card-warn">
          <div className="st-card-label">Upline managers</div>
          <div className="st-card-value warn">{(s?.upline_managers ?? 0).toLocaleString()}</div>
          <div className="tiny dim">{(s?.brand_origins ?? 0).toLocaleString()} brand origins</div>
        </div>
        <div className="st-card">
          <div className="st-card-label">Agents with mobile + IP</div>
          <div className="st-card-value">{(s?.agents_with_login_evidence ?? 0).toLocaleString()}</div>
          <div className="tiny dim">{(s?.mobiles_shared_by_multiple_agents ?? 0).toLocaleString()} mobiles shared by 2+ agents</div>
        </div>
      </div>

      <div className="money-rail-tabs">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            className={`ex-toggle ${view === v.id ? 'ex-toggle-on' : ''}`}
            onClick={() => setView(v.id)}
          >{v.label}</button>
        ))}
      </div>

      {loading && !data && <div className="dim">Loading…</div>}

      {view === 'desks' && (
        <table className="tbl">
          <thead><tr>
            <th>Operator console</th><th className="num">Rows</th><th className="num">Clients</th>
            <th className="num">Customers</th><th className="num">Deposit amount</th>
            <th className="num">Test rows</th><th>First seen</th><th>Last seen</th>
          </tr></thead>
          <tbody>
            {(data?.operator_desks ?? []).map((d) => (
              <tr key={d.operator}>
                <td className="mono">{d.operator}</td>
                <td className="num">{d.rows.toLocaleString()}</td>
                <td className="num">{d.clients}</td>
                <td className="num">{d.customers.toLocaleString()}</td>
                <td className="num money">{fmtINR(d.deposit_amount_inr)}</td>
                <td className="num">{(d.test_rows ?? 0).toLocaleString()}</td>
                <td className="tiny">{d.first_seen || '—'}</td>
                <td className="tiny">{d.last_seen || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {view === 'agents' && (
        <>
          <p className="tiny dim">{data?.roster_note}</p>
          <table className="tbl">
            <thead><tr>
              <th>Agent username</th><th>Mobile</th><th>Upline manager</th>
              <th>Brand origin</th><th className="num">Upline subtree</th>
            </tr></thead>
            <tbody>
              {(data?.items ?? []).map((a) => (
                <tr key={a.username}>
                  <td className="mono">{a.username}</td>
                  <td className="mono">{(a.mobiles ?? []).join(', ') || '—'}</td>
                  <td className="mono dim">{(a.upline_managers ?? []).join(', ') || '—'}</td>
                  <td className="tiny dim">{(a.origins ?? []).join(', ') || '—'}</td>
                  <td className="num">{(a.child_accounts ?? 0).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager page={page} pages={pages} setPage={setPage} total={data?.total ?? 0} />
        </>
      )}

      {view === 'managers' && <Ranked rows={data?.top_upline_managers ?? []} unit="agents reporting to" />}
      {view === 'brands' && <Ranked rows={data?.top_brand_origins ?? []} unit="agents on" />}

      {view === 'login' && (
        <>
          <p className="tiny dim">{data?.guard_rail}</p>
          <h3>Agents with a login record in public exports</h3>
          <table className="tbl">
            <thead><tr>
              <th>Agent</th><th>Mobile</th><th>Brand / upline</th>
              <th className="num">Logins</th><th>IP</th><th>Location</th><th>ISP</th>
            </tr></thead>
            <tbody>
              {(data?.login_evidence ?? []).map((r) => (
                <tr key={r.username}>
                  <td className="mono">{r.username}</td>
                  <td className="mono">{r.mobile || '—'}</td>
                  <td className="tiny dim">{r.brand || '—'}{r.upline_manager ? ` / ${r.upline_manager}` : ''}</td>
                  <td className="num">{r.logins ?? 0}</td>
                  <td className="mono">{r.ip || '—'}</td>
                  <td className="tiny">{[r.city, r.region].filter(Boolean).join(', ') || '—'}</td>
                  <td className="tiny dim">{r.isp || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Staff-looking bettor accounts — NOT operators</h3>
          <table className="tbl">
            <thead><tr><th>Username</th><th className="num">Logins</th><th>IPs</th><th>City</th><th>Note</th></tr></thead>
            <tbody>
              {(data?.staff_like_bettors ?? []).map((r) => (
                <tr key={r.username}>
                  <td className="mono">{r.username}</td>
                  <td className="num">{r.logins ?? 0}</td>
                  <td className="mono tiny">{(r.ips ?? []).join(', ')}</td>
                  <td className="tiny">{r.city || '—'}</td>
                  <td className="tiny dim">{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {view === 'corpora' && (
        <>
          <h3>Refuted hypotheses</h3>
          <table className="tbl">
            <thead><tr><th>ID</th><th>Claim</th><th>Result</th><th>Evidence</th></tr></thead>
            <tbody>
              {(data?.negative_results ?? []).map((n) => (
                <tr key={n.id}>
                  <td className="mono">{n.id}</td>
                  <td>{n.claim}</td>
                  <td className="mono">{n.result}</td>
                  <td className="tiny dim">{n.evidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Unmined public exports</h3>
          <table className="tbl">
            <thead><tr><th>Pattern</th><th className="num">Files</th><th className="num">Size (MB)</th><th>Priority</th></tr></thead>
            <tbody>
              {(data?.unmined_corpora ?? []).map((c) => (
                <tr key={c.pattern}>
                  <td className="mono">{c.pattern}</td>
                  <td className="num">{(c.files ?? c.indexed ?? 0).toLocaleString()}</td>
                  <td className="num">{c.size_mb ? c.size_mb.toLocaleString() : '—'}</td>
                  <td className="tiny">{c.priority || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="tiny dim">Roster source: {data?.roster_source || '—'} · collected {data?.roster_collected_utc || '—'}</p>
        </>
      )}
    </div>
  )
}

function Ranked({ rows, unit }: { rows: Counted[]; unit: string }) {
  if (!rows.length) return <div className="dim">None in roster artifact.</div>
  const max = Math.max(...rows.map((r) => r.agents))
  return (
    <table className="tbl">
      <thead><tr><th>Name</th><th className="num">{unit}</th><th style={{ width: '40%' }}>Share</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.name}>
            <td className="mono">{r.name}</td>
            <td className="num">{r.agents.toLocaleString()}</td>
            <td>
              <div className="hbar" style={{ height: 10 }}>
                <div className="bar" style={{ width: `${Math.max(2, (r.agents / max) * 100)}%` }} />
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Pager({ page, pages, setPage, total }: { page: number; pages: number; setPage: (n: number) => void; total: number }) {
  return (
    <div className="row-gap" style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 10 }}>
      <button className="ex-page-btn" disabled={page === 0} onClick={() => setPage(0)}>« first</button>
      <button className="ex-page-btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
      <span className="tiny dim">page {page + 1} / {pages} · {total.toLocaleString()} agents</span>
      <button className="ex-page-btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next →</button>
    </div>
  )
}