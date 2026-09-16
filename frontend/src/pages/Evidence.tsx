import { useCallback, useEffect, useState } from 'react'
import { api, post, tabHidden } from '../lib/api'
import { fmtBytes, fmtTime } from '../lib/format'

interface Job {
  job_id: string; label: string; total: number; done: number; failed: number; bytes: number; status: string; queued_utc: string; outdir: string
}
interface LogEntry {
  utc: string; event: string; source_key?: string; artifact_path?: string; artifact_sha256?: string; counts?: any
}

export function Evidence() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [log, setLog] = useState<LogEntry[]>([])
  const [exhibit, setExhibit] = useState<any>(null)
  const [keysText, setKeysText] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    api<any>('/api/download/jobs').then((d) => setJobs(d.jobs ?? [])).catch(() => {})
    api<any>('/api/evidence/log', { tail: 30 }).then((d) => setLog(d.tail ?? [])).catch(() => {})
    api<any>('/api/evidence/exhibit').then(setExhibit).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => { const t = setInterval(() => { if (!tabHidden()) refresh() }, 3000); return () => clearInterval(t) }, [refresh])

  async function queue() {
    const keys = keysText.split('\n').map((s) => s.trim()).filter(Boolean)
    if (!keys.length) return
    setBusy(true)
    try { await post('/api/download', { keys, label: 'manual' }); setKeysText(''); refresh() }
    catch (e) { alert(String(e)) }
    finally { setBusy(false) }
  }

  const activeJobs = jobs.filter((j) => j.status !== 'complete')
  const doneJobs = jobs.filter((j) => j.status === 'complete')

  function downloadCustodyCsv() {
    // Officer exhibit: the chain-of-custody tail as an Excel-ready CSV.
    api<any>('/api/evidence/log', { tail: 2000 }).then((d) => {
      const rows: LogEntry[] = d.tail ?? []
      const esc = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`
      const csv = ['utc,event,source_key,artifact_path,artifact_sha256']
        .concat(rows.map((e) => [e.utc, e.event, e.source_key ?? '', e.artifact_path ?? '', e.artifact_sha256 ?? ''].map(esc).join(',')))
        .join('\n')
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
      a.download = `chain-of-custody-${new Date().toISOString().slice(0, 10)}.csv`
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 5000)
    }).catch((e) => alert(String(e)))
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">⛨</span>
          <div>
            <h1 className="pg-header-title">Evidence & Downloads</h1>
            <div className="pg-header-sub">Every fetch is sha256-hashed with case authorization reference</div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={downloadCustodyCsv} title="Chain-of-custody log as CSV — officer exhibit, opens in Excel" style={{ fontSize: 11 }}>⬇ Custody log CSV</button>
        </div>
      </div>

      {/* Case exhibit manifest — what this tab is FOR: prosecution-ready framing */}
      {exhibit && (
        <div className="st-panel">
          <div className="st-panel-head">
            <span className="st-panel-title">Case exhibit manifest</span>
            <span className="small dim">generated {exhibit.generated_ist} ({exhibit.generated_utc})</span>
          </div>
          <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', fontSize: 12 }}>
            <span><span className="tiny dim">CASE </span><b className="mono">{exhibit.case_id}</b></span>
            <span><span className="tiny dim">AUTH </span><b className="mono">{exhibit.authorization_ref}</b></span>
            <span><span className="tiny dim">CUSTODY EVENTS </span><b>{(exhibit.custody_events_total ?? 0).toLocaleString()}</b></span>
            <span><span className="tiny dim">SHA256 ARTIFACTS </span><b>{(exhibit.artifacts_sha256 ?? 0).toLocaleString()}</b></span>
            <span><span className="tiny dim">DOWNLOADS </span><b>{exhibit.download_jobs_complete}/{exhibit.download_jobs} complete</b></span>
            <span><span className="tiny dim">OBJECTS </span><b>{(exhibit.objects_indexed ?? 0).toLocaleString()}</b></span>
          </div>
        </div>
      )}

      {/* Queue input */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Queue a download job</span>
        </div>
        <textarea
          rows={3}
          style={{ width: '100%', background: 'var(--bg)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 8, padding: 10, fontSize: 12, fontFamily: 'monospace', resize: 'vertical' }}
          placeholder={'Paste original object keys, one per line…\nbankingqr/178708.../qr.jpg\ndeposit/.../slip.png'}
          value={keysText}
          onChange={(e) => setKeysText(e.target.value)}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
          <button className="btn btn-primary" disabled={busy || !keysText.trim()} onClick={queue} style={{ fontSize: 12 }}>
            ⛨ Queue with evidence logging
          </button>
          <span className="small dim">Max 5000 keys/job</span>
        </div>
      </div>

      {/* Active jobs */}
      {activeJobs.length > 0 && (
        <div className="st-panel">
          <div className="st-panel-head">
            <span className="st-panel-title">Active downloads</span>
            <span className="ex-badge ex-badge-money">{activeJobs.length} running</span>
          </div>
          {activeJobs.map((j) => {
            const pct = j.total > 0 ? (j.done / j.total) * 100 : 0
            return (
              <div key={j.job_id} className="ev-job-card">
                <span className="ev-job-icon">📥</span>
                <div className="ev-job-info">
                  <div className="ev-job-id">{j.job_id}</div>
                  <div className="ev-job-label">{j.label}</div>
                </div>
                <div className="ev-job-progress">
                  <div className="ev-job-bar"><div className="ev-job-bar-fill" style={{ width: `${pct}%` }} /></div>
                  <span className="ev-job-counts">{j.done}/{j.total}</span>
                  {j.failed > 0 && <span style={{ color: 'var(--bad)', fontSize: 11 }}>({j.failed} failed)</span>}
                </div>
                <span className="ev-job-meta">{fmtBytes(j.bytes)}</span>
              </div>
            )
          })}
        </div>
      )}

      {/* Completed jobs */}
      <div className="st-panel">
        <div className="st-panel-head">
          <span className="st-panel-title">Download jobs</span>
          <span className="small dim">{doneJobs.length} done · {jobs.length} total (latest 10 listed)</span>
        </div>
        {doneJobs.length === 0 && activeJobs.length === 0 && (
          <div className="dp-empty"><div className="dp-empty-icon">📦</div>No jobs yet. Queue keys above or use Explorer.</div>
        )}
        {doneJobs.slice(0, 10).map((j) => (
          <div key={j.job_id} className="ev-job-card">
            <span className="ev-job-icon">✅</span>
            <div className="ev-job-info">
              <div className="ev-job-id">{j.job_id}</div>
              <div className="ev-job-label">{j.label} · {fmtTime(j.queued_utc)}</div>
            </div>
            <div className="ev-job-progress">
              <div className="ev-job-bar"><div className="ev-job-bar-fill done" style={{ width: '100%' }} /></div>
              <span className="ev-job-counts">{j.done}/{j.total}</span>
            </div>
            <span className="ev-job-meta">{fmtBytes(j.bytes)}</span>
          </div>
        ))}
      </div>

      {/* Chain of custody log */}
      <div className="st-panel" style={{ marginBottom: 0 }}>
        <div className="st-panel-head">
          <span className="st-panel-title">Chain-of-custody log</span>
        </div>
        {log.length === 0 && <div className="dp-empty"><div className="dp-empty-icon">📋</div>No evidence events yet.</div>}
        {log.map((e, i) => (
          <div key={i} className="ev-log-row">
            <span className="ev-log-time">{fmtTime(e.utc)}</span>
            <span className="ev-log-event">{e.event}</span>
            <span className="ev-log-artifact">{(e.source_key || e.artifact_path || '').slice(-60)}</span>
            <span className="ev-log-sha">{e.artifact_sha256 ? e.artifact_sha256.slice(0, 16) + '…' : '—'}</span>
          </div>
        ))}
      </div>
    </>
  )
}
