import { useEffect, useRef, useState } from 'react'
import { objectUrl, voiceSrc } from '../lib/api'
import { fmtBytes } from '../lib/format'

function fmtClock(s: number): string {
  if (!isFinite(s) || s < 0) return '0:00'
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${String(sec).padStart(2, '0')}`
}

/** Lazy single-play voice-note player (voice 0:00/delay fix 2026-09-12).
 *
 * Old design mounted 50x <audio preload="metadata"> per Chat page, so the
 * browser fired 50 simultaneous /api/object fetches — each cold MISS buffers
 * a full GCS fetch before responding. With ~6 parallel connections the tail
 * clips stalled for seconds ("very delay"), and AMR/3GP clips rendered a
 * dead 0:00 player with no recovery path.
 *
 * This player:
 * - sets NO src until the row scrolls into view (IntersectionObserver) or
 *   the officer presses play — the Chat list no longer DDoSes itself;
 * - picks a server-transcoded ?format=wav URL when the browser cannot decode
 *   the native codec (AMR/3GP always; WebM/Opus on Safari);
 * - pauses every other voice note when one starts (single-play);
 * - surfaces loading / error / retry states explicitly instead of 0:00.
 */
export function VoiceNote({ objKey, size, compact }: { objKey: string; size?: number | null; compact?: boolean }) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  const [inView, setInView] = useState(false)
  const [src, setSrc] = useState('')
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [playing, setPlaying] = useState(false)
  const [cur, setCur] = useState(0)
  const [dur, setDur] = useState(0)
  const [triedWav, setTriedWav] = useState(false)
  const [retryN, setRetryN] = useState(0)

  const smart = voiceSrc(objKey)

  // Reveal src when the row enters the viewport (preload="none" still, so
  // the browser only fetches metadata — not the full clip — until play).
  useEffect(() => {
    const el = wrapRef.current
    if (!el) { setInView(true); return }
    if (typeof IntersectionObserver === 'undefined') { setInView(true); return }
    const io = new IntersectionObserver(
      (es) => { for (const e of es) if (e.isIntersecting) { setInView(true); io.disconnect() } },
      { rootMargin: '400px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [objKey])

  useEffect(() => {
    setSrc(''); setState('idle'); setPlaying(false)
    setCur(0); setDur(0); setTriedWav(smart.includes('format=wav')); setInView(false)
    // re-arm intersection for the new key
    setInView(true)
  }, [objKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (inView && !src) {
      setSrc(retryN ? smart + (smart.includes('?') ? '&' : '?') + `_r=${retryN}` : smart)
      setState('loading')
    }
  }, [inView, src, smart, retryN])

  // Single-play: when any voice note starts, pause the rest.
  useEffect(() => {
    const el = audioRef.current
    if (!el) return
    const onPlay = () => {
      window.dispatchEvent(new CustomEvent('voicenote-play', { detail: objKey }))
    }
    const onPauseOther = (e: Event) => {
      if ((e as CustomEvent).detail !== objKey && !el.paused) el.pause()
    }
    el.addEventListener('play', onPlay)
    window.addEventListener('voicenote-play', onPauseOther)
    return () => {
      el.removeEventListener('play', onPlay)
      window.removeEventListener('voicenote-play', onPauseOther)
    }
  }, [objKey, src])

  function onLoadedMeta() {
    const el = audioRef.current
    if (el && isFinite(el.duration) && el.duration > 0) setDur(el.duration)
    setState('ready')
  }

  function onError() {
    // First failure on a native URL: fall back to the transcoded WAV once
    // (covers misnamed .wav-that-is-WebM on browsers whose probe lied).
    if (!triedWav && !smart.includes('format=wav')) {
      setTriedWav(true)
      setSrc(objectUrl(objKey, false, 0, 'wav'))
      setState('loading')
      return
    }
    setState('error')
    setPlaying(false)
  }

  function retry() {
    setState('loading')
    setRetryN((n) => n + 1)
    setSrc('')
    setTriedWav(smart.includes('format=wav'))
  }

  const showSeek = state === 'ready' || playing || dur > 0

  return (
    <div className={`ch-audio-wrap vn-wrap${state === 'error' ? ' ch-audio-err' : ''}`} ref={wrapRef}>
      <span style={{ fontSize: 14 }} aria-hidden>🎙️</span>
      {state === 'error' ? (
        <>
          <span style={{ fontSize: 12 }}>⚠</span>
          <span className="tiny dim">can't preview this clip</span>
          <button className="btn" style={{ padding: '2px 8px', fontSize: 11, height: 24 }} onClick={retry} title="Retry preview">↻ Retry</button>
          <a className="btn" style={{ padding: '2px 8px', fontSize: 11, height: 24 }} href={objectUrl(objKey, true)} download title="Download with evidence hash">⛨ Download</a>
        </>
      ) : (
        <>
          {(!inView || !src) ? (
            <span className="tiny dim">🎙 voice note{size ? ` · ${fmtBytes(size)}` : ''} — press play</span>
          ) : state === 'loading' && !playing && cur === 0 ? (
            <span className="tiny dim" aria-live="polite">⏳ loading…{size ? ` ${fmtBytes(size)}` : ''}</span>
          ) : null}
          {src && (
            <audio
              ref={audioRef}
              className="ch-audio"
              controls
              preload="none"
              src={src}
              onLoadedMetadata={onLoadedMeta}
              onCanPlay={() => setState('ready')}
              onError={onError}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onTimeUpdate={(e) => {
                const el = e.currentTarget
                setCur(el.currentTime)
                if (isFinite(el.duration) && el.duration > 0) setDur(el.duration)
              }}
              style={compact ? { flex: 1 } : undefined}
            />
          )}
          {showSeek && dur > 0 && (
            <span className="tiny dim mono" style={{ whiteSpace: 'nowrap' }}>
              {fmtClock(cur)} / {fmtClock(dur)}
            </span>
          )}
          {state !== 'loading' && (
            <a
              className="btn"
              style={{ padding: '2px 8px', fontSize: 11, height: 24, flexShrink: 0 }}
              href={objectUrl(objKey, true)}
              download
              title="Download with evidence hash"
            >
              ⛨
            </a>
          )}
          {(state === 'loading' || (state === 'ready' && dur === 0 && !playing)) && (
            <button className="btn" style={{ padding: '2px 8px', fontSize: 11, height: 24 }} onClick={retry} title="Reload preview">↻</button>
          )}
        </>
      )}
      {size != null && state !== 'error' && <span className="tiny dim mono" style={{ flexShrink: 0 }}>{fmtBytes(size)}</span>}
    </div>
  )
}
