// Every state the Screen page can be in must tell the user what to do: a hint, an "idle" instruction, or a button
// (approve / set up / start / take over). Runs the REAL describe() of both front ends (hermes-webui extension and the
// dashboard tab), extracted from the shipped files, against a table of states.   node tests/ui/guidance.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

function extract(src, header) {
  const i = src.indexOf(header)
  assert.ok(i >= 0, 'describe() not found: ' + header)
  let depth = 0, j = src.indexOf('{', i)
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') depth++
    else if (src[k] === '}' && --depth === 0) return src.slice(i, k + 1)
  }
  throw new Error('unbalanced')
}
const webuiSrc = readFileSync(new URL('../../webui/assets/takeover.js', import.meta.url), 'utf8')
const dashSrc = readFileSync(new URL('../../dashboard/dist/index.js', import.meta.url), 'utf8')
// WebUI: describe() reads the closure's S, AC and prof(); give it its own
const webuiDescribe = new Function('S', 'AC', 'prof', extract(webuiSrc, 'function describe() {') + '\nreturn describe();')
const dashDescribe = new Function('s', extract(dashSrc, 'function describe(s) {') + '\nreturn describe(s);')

const ready = { supported: true, ready: true, headed: true }
const notReady = { supported: true, ready: false, headed: false }
const st = (o) => Object.assign({ readiness: ready, setup: { state: 'done', auto_allowed: true }, screen: { running: true }, lease: { holder: 'agent' } }, o)
const now = Date.now() / 1000

const STATES = {
  'not installed in this profile':   { err: { error: 'not_installed' } },
  'WebUI password missing':           { err: { http: 403 }, ac: { tried: true, error: 'nopassword' }, webuiOnly: true },
  'WebUI consent missing':            { err: { http: 403 }, ac: { tried: true }, webuiOnly: true, want: { approve: true, hintHas: 'Approve proxy consent' } },
  'service down':                     { err: { http: 502, error: 'unreachable' }, webuiOnly: true, want: { copy: 'hermes takeover webui' } },
  'host not supported':               { status: st({ readiness: { supported: false } }) },
  'setup running':                    { status: st({ readiness: notReady, setup: { state: 'running', auto_allowed: true } }), want: { hintHas: 'minute' } },
  'setup needs a manual command':     { status: st({ readiness: notReady, setup: { state: 'needs_manual', error: 'Needs root.', manual_command: 'sudo apt-get install -y tigervnc-standalone-server', auto_allowed: true } }), want: { copy: 'sudo apt-get install -y tigervnc-standalone-server', retry: true } },
  'setup failed':                     { status: st({ readiness: notReady, setup: { state: 'failed', error: 'boom', auto_allowed: true } }), want: { retry: true } },
  'automatic setup switched off':     { status: st({ readiness: notReady, setup: { state: 'idle', auto_allowed: false } }), want: { retry: true, hintHas: 'hermes takeover setup' } },
  'setup tried before, still missing': { status: st({ readiness: notReady, setup: { state: 'idle', auto_allowed: true, attempted: true } }), want: { retry: true } },
  'screen failed to start':           { status: st({ screen: { running: false, start_error: 'Xvnc died' } }), want: { start: true } },
  'screen off':                       { status: st({ screen: { running: false } }), want: { start: true } },
  'you are driving':                  { status: st({ lease: { holder: 'human', mine: true } }), want: { idleHas: 'Hand back' } },
  'agent driving':                    { status: st({}), want: { idleHas: 'Take over' } },
  'agent driving, setup just done':   { status: st({ setup: { state: 'done', finished: now - 60, auto_allowed: true } }), want: { idleHas: 'empty' } },
}
// Transient states that resolve by themselves (the dot says "wait"); they need no instruction.
const TRANSIENT = new Set(['wait'])

let n = 0, bad = 0
for (const [name, c] of Object.entries(STATES)) {
  const runs = [['webui', () => webuiDescribe({ err: c.err || null, status: c.status || null, starting: false },
                                             Object.assign({ tried: true, busy: false, error: null }, c.ac || {}), () => 'default')]]
  if (!c.webuiOnly) runs.push(['dashboard', () => dashDescribe({ err: c.err && c.err.error === 'not_installed' ? null : (c.err || null), status: c.err ? null : (c.status || null), starting: false })])
  for (const [ui, run] of runs) {
    if (ui === 'dashboard' && c.err) continue          // the dashboard has no WebUI consent / profiles / service states
    const d = run()
    try {
      const actionable = !!(d.hint || d.idle || d.retry || d.start || d.approve || d.copy)
      assert.ok(actionable || (TRANSIENT.has(d.dot) && !c.want), `no instruction and no button: ${JSON.stringify(d)}`)
      const w = c.want || {}
      if (w.approve) assert.equal(d.approve, true)
      if (w.retry) assert.equal(d.retry, true, 'needs a "Set up the screen" button')
      if (w.start) assert.equal(d.start, true, 'needs a "Start screen" button')
      if (w.copy) assert.equal(d.copy, w.copy, 'needs the exact command with a Copy button')
      if (w.hintHas) assert.ok((d.hint || '').includes(w.hintHas), `hint should mention "${w.hintHas}": ${d.hint}`)
      if (w.idleHas) assert.ok((d.idle || '').includes(w.idleHas), `instruction should mention "${w.idleHas}": ${d.idle}`)
      n++; console.log(`ok  [${ui}] ${name}: ${d.text}`)
    } catch (e) { bad++; console.log(`FAIL [${ui}] ${name}\n     ${e.message}`) }
  }
}
// the buttons the hints name must exist with those labels
for (const [ui, src, labels] of [['webui', webuiSrc, ['Set up the screen', 'Allow Takeover', 'Take over', 'Hand back', 'Copy command']],
                                 ['dashboard', dashSrc, ['Set up the screen', 'Start screen', 'Take over', 'Hand back', 'Copy command']]]) {
  for (const l of labels) {
    try { assert.ok(src.includes('>' + l + '<') || src.includes('"' + l + '"') || src.includes("'" + l + "'"), `${ui} has no "${l}" button`); n++ }
    catch (e) { bad++; console.log('FAIL ' + e.message) }
  }
}
console.log(`${n} passed` + (bad ? `, ${bad} FAILED` : ''))
process.exitCode = bad ? 1 : 0
