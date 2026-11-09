// dow-voice-chime — speak each assistant reply in a Dawn of War character voice.
// Bottom-bar chip (right cluster, next to the Ollama/Strata unloader): click opens a picker.
// Voices are folders under hermes/tts-voices/<id>/ (ref.wav + ref_text.txt + voice.json);
// synthesis + playback go through hermes/tools/dow_tts.py.
// v3 notes: v2 died with React #130 because icons.Loader2 does not exist here (the SDK's
// `icons` is @tabler/icons-react, not lucide). So: icons are resolved defensively and the
// picker is a plain fixed-position div, not a Radix popover (narrow hard-clipped chrome).
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'
import { atom, host, icons, STATUSBAR_AREAS, useValue } from '@hermes/plugin-sdk'

const PY = 'C:/Users/Sander/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe'
const HELPER = 'C:/Users/Sander/AppData/Local/hermes/tools/dow_tts.py'
const SPOOL = 'C:/Users/Sander/AppData/Local/hermes/tools/dow_spool.py'
const SAFE_ID = /^[A-Za-z0-9_-]{1,32}$/
const MAX_CHUNK = 320
const MAX_CHUNKS = 8

const voicesAtom = atom([])
const voiceAtom = atom('')
const statusAtom = atom('idle')
const diagAtom = atom('')
const engineAtom = atom({ precision: 'auto', workers: 3, live: null })
let store = null
let statusTimer = null
let last = { text: '', at: 0 }

const Ic = name => (icons && icons[name] ? icons[name] : null)
const q = s => '"' + String(s).replace(/"/g, '') + '"'
const b64 = s => btoa(unescape(encodeURIComponent(s)))

function chunks(text) {
  const clean = String(text || '')
    .replace(/```[\s\S]*?```/g, ' [code block] ')
    .replace(/`[^`\n]*`/g, ' code ')
    .replace(/\s+/g, ' ').trim()
  if (!clean) return []
  const parts = clean.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [clean]
  const out = []
  let cur = ''
  for (const p of parts) {
    if (cur.length + p.length > MAX_CHUNK && cur) { out.push(cur.trim()); cur = '' }
    cur += p
    if (cur.length > MAX_CHUNK * 2) { out.push(cur.slice(0, MAX_CHUNK * 2).trim()); cur = '' }
  }
  if (cur.trim()) out.push(cur.trim())
  return out.slice(0, MAX_CHUNKS)
}

// Mirrors the working ollama/strata plugins: positive `code === 0` check, never
// `code !== 0` (a missing/string code must not read as an error).
function readStdout(res) {
  if (res == null || typeof res !== 'object') return { err: 'no gateway response: ' + String(JSON.stringify(res)).slice(0, 120) }
  if (res.code === 0) return { out: String(res.stdout || '').trim() }
  return { err: String(res.stderr || res.stdout || 'exit ' + res.code).slice(0, 200).trim() }
}

async function run(command) {
  // The desktop socket is canonical: shell.exec there requires {session_id, command}
  // (tui_gateway/contracts/tools_commands.py CanonicalShellExecParams). A bare
  // {command} — which older plugins send — is rejected as invalid params. Try the
  // canonical shape first, then legacy, and report both messages if both fail.
  const sid = (host.state.focusedSessionId && host.state.focusedSessionId.get()) ||
    (host.state.activeSessionId && host.state.activeSessionId.get()) || null
  const tries = []
  if (sid) tries.push(['canonical', { session_id: sid, command }])
  tries.push(['legacy', { command }])
  const errs = []
  for (const shape of tries) {
    try {
      return readStdout(await host.request('shell.exec', shape[1]))
    } catch (e) {
      errs.push(shape[0] + ': ' + String((e && e.message) || e).slice(0, 110))
    }
  }
  return { err: ('shell.exec rejected — ' + errs.join(' | ')).slice(0, 260) }
}

async function pool(what) {
  // Voice on => the shared-weight holder and its 3 IPC workers spool up; off => they are
  // torn down and the ~4 GB of parked VRAM is handed back (41.1 s vs 56.4 s per reply).
  const r = await run(q(PY) + ' ' + q(HELPER) + ' pool ' + what)
  if (r.err) { diagAtom.set('pool ' + what + ': ' + r.err); return r }
  const line = String(r.out || '').trim().split('\n').pop() || ''
  host.notify({ kind: 'success', message: 'dow voice pool ' + what + ': ' + line.slice(0, 90) })
  return r
}

async function loadSettings() {
  const r = await run(q(PY) + ' ' + q(HELPER) + ' settings')
  if (r.err) { diagAtom.set('settings: ' + r.err); return }
  try { engineAtom.set(JSON.parse(String(r.out || '').trim())) }
  catch (e) { diagAtom.set('settings json: ' + e) }
}

// precision and worker count are frozen when the holder loads the weights, so the change is
// written to tts-voices/pool.json and the helper restarts the pool in the same call.
async function setSetting(kind, value) {
  const r = await run(q(PY) + ' ' + q(HELPER) + ' setting --' + kind + ' ' + value)
  if (r.err) { diagAtom.set('setting ' + kind + ': ' + r.err); return }
  const line = String(r.out || '').trim().split('\n').pop() || ''
  host.notify({ kind: 'success', message: 'reply engine: ' + line.slice(0, 110) })
  void loadSettings()
}

async function refresh() {
  const r = await run(q(PY) + ' ' + q(HELPER) + ' voices')
  let list = null
  if (!r.err) {
    try {
      const parsed = JSON.parse(r.out || '[]')
      if (Array.isArray(parsed)) list = parsed
      else diagAtom.set('voices returned non-array: ' + String(r.out).slice(0, 120))
    } catch (e) {
      diagAtom.set('parse failed: ' + String(e && e.message || e).slice(0, 100) + ' | raw: ' + String(r.out).slice(0, 120))
    }
  } else {
    diagAtom.set(r.err)
  }
  if (list) {
    voicesAtom.set(list)
    if (!list.length) diagAtom.set('helper printed [] — check ' + HELPER)
  }
  // Renderer console lines land in logs/desktop.log as [renderer console:main].
  console.log('[dow-voice-chime] voices refresh: n=' + (list ? list.length : 'null') +
    ' err=' + (r.err || 'none') + ' rawlen=' + String(r.out || '').length +
    ' PY=' + PY + ' HELPER=' + HELPER)
  statusAtom.set(list && list.length ? 'idle' : 'err')
}

function usable() {
  return (voicesAtom.get() || []).filter(v => v && v.ready !== false && SAFE_ID.test(String(v.id)))
}

function setVoice(id) {
  voiceAtom.set(id)
  if (store) store.set('voice', id)
  const v = usable().find(x => x.id === id)
  host.notify({ kind: 'success', message: v ? 'reply voice: ' + (v.name || v.id) : 'reply voice: off' })
  void pool(id ? 'up' : 'down')     // the pool follows the voice selection
}

function stopVoice() {
  setVoice('')
  void run(q(PY) + ' ' + q(SPOOL) + ' cancel')   // bump token: the running spooler stops at its next chunk
}

function cycleVoice(back) {
  const order = ['', ...usable().map(v => v.id)]
  const at = order.indexOf(voiceAtom.get())
  setVoice(order[(at + (back ? order.length - 1 : 1)) % order.length])
}

async function speak(text, sid) {
  const vid = voiceAtom.get()
  if (!vid) return
  const focused = host.state.focusedSessionId ? host.state.focusedSessionId.get() : null
  if (sid && focused && sid !== focused) return
  if (last.text === text && Date.now() - last.at < 4000) return
  last = { text, at: Date.now() }
  const parts = chunks(text)
  if (!parts.length) return
  // ONE gateway call per reply. The spooler synthesizes every chunk, splices them in index
  // order into a single stream (280 ms pauses), matches that character's measured pace, and
  // plays it with one mouth — so no overlapping audio and no console per chunk. It also bumps
  // the generation token, so a spooler left running from an older reply aborts between chunks.
  const r = await run(q(PY) + ' ' + q(SPOOL) + ' start --voice ' + vid + ' --b64 ' + b64(parts.join(' ')))
  const line = (r.out || '').split('\n').pop() || ''
  if (!/^ok /.test(line)) {
    statusAtom.set('err')
    host.notify({ kind: 'warning', message: 'dow voice: ' + String(line || r.err || 'spooler refused').slice(0, 160) })
    return
  }
  statusAtom.set('play')
  clearTimeout(statusTimer)
  statusTimer = setTimeout(() => statusAtom.set('idle'), 4 * 60 * 1000)
}

const panelStyle = {
  position: 'fixed', right: '12px', bottom: '34px', zIndex: 9999,
  display: 'flex', flexDirection: 'column', minWidth: '238px',
  background: 'var(--card, #1c1c1e)', border: '1px solid var(--border, #333)',
  borderRadius: '7px', padding: '4px', color: 'var(--foreground, #e6e6e6)',
  font: '12px system-ui', boxShadow: '0 8px 24px rgba(0,0,0,.35)',
}
const rowStyle = {
  display: 'flex', gap: '8px', alignItems: 'center', width: '100%',
  padding: '5px 8px', border: '0', borderRadius: '5px', background: 'transparent',
  color: 'inherit', font: 'inherit', textAlign: 'left', cursor: 'pointer',
}

function Chip() {
  const voices = useValue(voicesAtom)
  const voice = useValue(voiceAtom)
  const status = useValue(statusAtom)
  const eng = useValue(engineAtom) || { precision: 'auto', workers: 3, live: null }
  const diag = useValue(diagAtom)
  const [open, setOpen] = useState(false)
  const panel = useRef(null)

  useEffect(() => {
    if (store) voiceAtom.set(store.get('voice', ''))
    if (voiceAtom.get()) void pool('up')   // a saved voice means the pool should be warm
    void loadSettings()
    void refresh()
    const iv = setInterval(() => void refresh(), 45000)
    return () => clearInterval(iv)
  }, [])

  useEffect(() => {
    if (!open) return
    const onDown = e => {
      if (panel.current && !panel.current.contains(e.target)) setOpen(false)
    }
    const onKey = e => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const list = (voices || []).filter(v => v && SAFE_ID.test(String(v.id)))
  const active = list.find(v => v.id === voice)
  const tag = active ? String(active.tag || active.name || active.id).slice(0, 3) : (list.length ? 'off' : '?')
  const Icon = Ic(active ? 'Volume2' : 'VolumeX')

  return jsxs('span', {
    style: { display: 'inline-flex', alignItems: 'center', position: 'relative' },
    children: [
      jsx('button', {
        title: 'Dawn of War reply voice — ' + (eng.live
          ? (eng.live.precision + ', ' + (eng.live.ports || []).length + ' workers, live')
          : (eng.precision + ', ' + eng.workers + ' workers, pool down')),
        onClick: () => { if (!open) void loadSettings(); setOpen(!open) },
        style: {
          display: 'inline-flex', alignItems: 'center', gap: '3px', padding: '2px 6px',
          border: '0', borderRadius: '5px', background: 'transparent', color: 'inherit',
          font: '10px system-ui', cursor: 'pointer',
        },
        children: jsxs('span', {
          style: { display: 'inline-flex', alignItems: 'center', gap: '3px' },
          children: [
            Icon ? jsx(Icon, { size: 12 }) : null,
            jsx('span', { children: status === 'err' ? '!' : tag }),
          ],
        }),
      }),
      open ? jsxs('div', {
        ref: panel,
        style: panelStyle,
        children: [
          jsx('div', { style: { fontSize: '10px', opacity: 0.6, padding: '4px 8px' }, children: 'Speak my replies as:' }),
          jsx('button', { style: rowStyle, onClick: () => { stopVoice(); setOpen(false) }, children: '— off —' }),
          ...list.map(v => jsxs('button', {
            style: { ...rowStyle, fontWeight: v.id === voice ? '700' : '400' },
            onClick: () => { setVoice(v.id); setOpen(false) },
            children: [
              jsx('span', { style: { width: '32px', opacity: 0.65 }, children: String(v.tag || v.id).slice(0, 3) }),
              jsx('span', { children: v.name || v.id }),
              v.ready === false ? jsx('span', { style: { marginLeft: 'auto', fontSize: '10px', opacity: 0.6 }, children: 'no audio' }) : null,
            ],
          }, v.id)),
          jsx('div', { style: { fontSize: '10px', opacity: 0.6, padding: '6px 8px 2px', borderTop: '1px solid var(--border, #333)', marginTop: '4px' }, children: 'Engine — a change restarts the pool:' }),
          ...['auto', 'fp32'].map(p => jsxs('button', {
            style: { ...rowStyle, fontWeight: eng.precision === p ? '700' : '400' },
            title: p === 'fp32'
              ? 'fp32: noticeably smoother on editing headphones; ~1.5x slower and the shared weight set costs 4.8 GB instead of 2.6, putting the card near 94%'
              : 'bf16 (auto): indistinguishable from fp32 on a bluetooth speaker, and it leaves VRAM headroom',
            onClick: () => { if (eng.precision !== p) void setSetting('precision', p); setOpen(false) },
            children: [
              jsx('span', { children: p === 'auto' ? 'bf16' : p }),
              jsx('span', { style: { marginLeft: 'auto', fontSize: '10px', opacity: 0.6 },
                            children: eng.live && eng.live.precision === p ? '✓ live' : (eng.precision === p ? '✓' : '') }),
            ],
          }, p)),
          jsx('div', { style: { fontSize: '10px', opacity: 0.6, padding: '4px 8px 0' }, children: 'Workers — parallel chunks:' }),
          jsxs('div', { style: { display: 'flex', gap: '1px', padding: '2px 8px 4px' }, children:
            [1, 2, 3, 4, 5, 6, 7].map(n => jsx('button', {
              style: { padding: '2px 7px', border: '0', borderRadius: '4px', background: eng.workers === n ? 'var(--accent, #333)' : 'transparent',
                       color: (eng.capacity && n > eng.capacity.max) ? 'var(--destructive, #e5484d)' : 'inherit',
                       font: '10px system-ui', cursor: 'pointer',
                       fontWeight: eng.workers === n ? '700' : ((eng.capacity && n > eng.capacity.max) ? '600' : '400'),
                       textDecoration: (eng.capacity && n > eng.capacity.max) ? 'line-through' : 'none' },
              title: String(n) + ' workers - ' + (eng.capacity ? eng.capacity.arithmetic : 'availability not measured yet'),
              onClick: () => { if (eng.workers !== n) void setSetting('workers', n); setOpen(false) },
              children: String(n),
            }, n)) }),
          jsx('div', { style: { fontSize: '10px', opacity: 0.6, padding: '4px 8px 0' }, children: 'Flavour lines — catch + persona sentence:' }),
          jsxs('div', { style: { display: 'flex', gap: '2px', padding: '2px 8px 4px' }, children:
            ['on', 'off'].map(v => jsx('button', {
              style: { padding: '2px 8px', border: '0', borderRadius: '4px',
                       background: (eng.flavor === false ? v === 'off' : v === 'on') ? 'var(--accent, #333)' : 'transparent',
                       color: 'inherit', font: '10px system-ui', cursor: 'pointer',
                       fontWeight: (eng.flavor === false ? v === 'off' : v === 'on') ? '700' : '400' },
              title: v === 'on' ? 'prepend the character catch and append the persona sentence to the first chunk of a reply'
                                : 'speak only the reply text; vocabulary swaps and tempo still apply, no pool restart',
              onClick: () => { void setSetting('flavor', v); setOpen(false) },
              children: v,
            }, v)) }),
          eng.live ? jsx('div', { style: { fontSize: '10px', opacity: 0.6, padding: '0 8px 2px' },
            children: 'live: ' + (eng.live.ports || []).join(' ') + ' ' + eng.live.precision
                      + (eng.capacity ? ' | fits up to ' + eng.capacity.max
                                      + ' (gpu allows ' + eng.capacity.limit_gpu
                                      + ', commit allows ' + eng.capacity.limit_commit + ')' : '') }) : null,
          jsx('button', {
            style: { ...rowStyle, borderTop: '1px solid var(--border, #333)', marginTop: '3px', opacity: 0.8 },
            onClick: () => { void refresh(); host.notify({ kind: 'success', message: 'reloaded ' + (voicesAtom.get() || []).length + ' voices' }) },
            children: jsxs('span', { style: { display: 'flex', width: '100%' }, children: [jsx('span', { children: 'reload list' }), jsx('span', { style: { marginLeft: 'auto', fontSize: '10px', opacity: 0.6 }, children: list.length + ' ready' })] }),
          }),
          diag ? jsx('div', { style: { fontSize: '10px', opacity: 0.75, padding: '5px 8px', borderTop: '1px solid var(--border, #333)', whiteSpace: 'pre-wrap' }, children: diag.slice(0, 220) }) : null,
        ],
      }) : null,
    ],
  })
}

export default {
  id: 'dow-voice-chime',
  name: 'Dawn of War reply voice',
  register(ctx) {
    store = ctx.storage
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 20, render: () => jsx(Chip, {}) })
    ctx.register({ id: 'next', area: 'palette', data: { id: 'next', label: 'Dawn of War voice: next voice', keywords: ['tts', 'voice', 'dow'], run: () => cycleVoice(false), detail: () => voiceAtom.get() || 'off', detailVariant: 'state', keepOpen: true } })
    ctx.register({ id: 'off', area: 'palette',      data: { id: 'off', label: 'Dawn of War voice: stop speaking', keywords: ['tts', 'mute'], run: () => stopVoice() } })
    ctx.register({ id: 'reload', area: 'palette', data: { id: 'reload', label: 'Dawn of War voice: reload voice list', keywords: ['tts', 'voices'], run: () => void refresh() } })
    const off = host.onEvent('message.complete', event => {
      const text = event && event.payload && event.payload.text
      if (!text) return
      void speak(String(text), event && event.session_id)
    })
    ctx.onDispose(() => off())
  },
}
