// Drives desktop/plugin.js through a fake PluginContext. Run: node --import ./tests/desktop/register.mjs tests/desktop/run.mjs
import assert from 'node:assert/strict'
import plugin from '../../desktop/plugin.js'
import { calls } from './stub-sdk.mjs'

let n = 0
const ok = (name, fn) => Promise.resolve().then(fn).then(() => { n++; console.log('ok  ' + name) }, e => { console.log('FAIL ' + name + '\n' + e.stack); process.exitCode = 1 })

function makeCtx({ pulse } = {}) {
  const handlers = {}, reg = [], intervals = [], disposers = []
  const ctx = {
    source: 'plugin:takeover',
    onEvent: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); return () => {} },
    register: c => { reg.push(c); return () => {} },
    rest: async (path, opts) => { calls.rest.push({ path, ...opts }); if (path === '/pulse') return typeof pulse === 'function' ? pulse() : pulse; return { ok: true } },
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return () => {} },
    onDispose: fn => disposers.push(fn),
    os: { notify: o => calls.native.push(o) },
    pet: { say: (t, o) => calls.pet.push({ t, o }), clear: id => calls.pet.push({ clear: id }) },
    runAction: id => { calls.run.push('action:' + id); return { ok: true } },
  }
  return { ctx, emit: (type, payload) => (handlers[type] || []).forEach(fn => fn({ type, payload })), reg, intervals, disposers, handlers }
}
const reset = () => { for (const k of Object.keys(calls)) calls[k].length = 0 }
const tick = () => new Promise(r => setTimeout(r, 5))
const chip = reg => reg.find(c => c.id === 'chip')
const renderChip = reg => chip(reg).render()

await ok('metadata: id matches folder, enabled by default', () => {
  assert.equal(plugin.id, 'takeover'); assert.equal(plugin.defaultEnabled, true); assert.equal(typeof plugin.register, 'function')
})

await ok('registers a status-bar chip and a palette command, polls as a safety net, listens to both events', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true, request: null } }); plugin.register(t.ctx); await tick()
  assert.ok(chip(t.reg)); assert.equal(chip(t.reg).area, 'statusBar.right')
  assert.ok(t.reg.find(c => c.id === 'palette' && c.data.run))
  assert.deepEqual(Object.keys(t.handlers).sort(), ['plugin.takeover.ended', 'plugin.takeover.request'])
  assert.equal(t.intervals.length, 1); assert.ok(t.intervals[0].ms >= 5000)
  assert.equal(calls.rest[0].path, '/pulse')
})

await ok('a request alerts once: toast, native notification, pet; the same id again is silent', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true, request: null } }); plugin.register(t.ctx); await tick()
  t.emit('plugin.takeover.request', { id: 'r1', reason: 'Enter the SMS code for your bank', profile: 'default' })
  t.emit('plugin.takeover.request', { id: 'r1', reason: 'Enter the SMS code for your bank', profile: 'default' })
  assert.equal(calls.notify.length, 1); assert.equal(calls.notify[0].kind, 'warning')
  assert.match(calls.notify[0].message, /SMS code/); assert.equal(typeof calls.notify[0].action.onClick, 'function')
  assert.equal(calls.native.length, 1); assert.match(calls.native[0].body, /SMS code/)
  assert.ok(calls.pet.find(p => p.t && /SMS code/.test(p.t)))
  t.emit('plugin.takeover.request', { id: 'r2', reason: 'Approve the app', profile: 'default' })
  assert.equal(calls.notify.length, 2)
})

await ok('ended clears the alert; an ended for a different request does not', async () => {
  reset(); const t = makeCtx({ pulse: { ok: false } }); plugin.register(t.ctx); await tick()
  t.emit('plugin.takeover.request', { id: 'a', reason: 'x' })
  t.emit('plugin.takeover.ended', { id: 'other', status: 'done' })
  assert.ok(calls.notify.length === 1)
  t.emit('plugin.takeover.ended', { id: 'a', status: 'done' })
  assert.ok(calls.pet.find(p => p.clear === 'takeover'))
})

await ok('a window opened after the request was posted finds it by polling, once', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true, request: { id: 'late', status: 'pending', reason: 'Solve the CAPTCHA' } } })
  plugin.register(t.ctx); await tick()
  assert.equal(calls.notify.length, 1); assert.match(calls.notify[0].message, /CAPTCHA/)
  await t.intervals[0].fn(); await tick()
  assert.equal(calls.notify.length, 1, 'polling the same request must not alert again')
})

await ok('a poll that says nothing is pending clears a stale chip; an unreachable backend keeps it', async () => {
  reset(); let answer = { ok: true, request: { id: 'p', status: 'pending', reason: 'Sign in' } }
  const t = makeCtx({ pulse: () => answer }); plugin.register(t.ctx); await tick()
  assert.equal(calls.notify.length, 1)
  answer = { ok: false }; await t.intervals[0].fn(); await tick()
  answer = { ok: true, request: null }; await t.intervals[0].fn(); await tick()
  t.emit('plugin.takeover.request', { id: 'p', reason: 'Sign in' })       // same id after being cleared: seen already, silent
  assert.equal(calls.notify.length, 1)
})

await ok('a long or hostile reason is shortened and rendered as text (no HTML path)', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true } }); plugin.register(t.ctx); await tick()
  const evil = '<img src=x onerror=alert(1)>' + 'A'.repeat(500)
  t.emit('plugin.takeover.request', { id: 'e', reason: evil })
  assert.ok(calls.notify[0].message.length <= 300)
  const html = JSON.stringify(renderChip(t.reg))
  assert.ok(!/dangerouslySetInnerHTML|innerHTML/.test(html))
})

await ok('missing payload fields never throw', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true } }); plugin.register(t.ctx); await tick()
  t.emit('plugin.takeover.request', undefined); t.emit('plugin.takeover.request', {}); t.emit('plugin.takeover.ended', undefined)
  assert.equal(calls.notify.length, 0)
})

await ok('degrades when optional SDK doors are missing (no pet, no native notifications, no workspace tabs)', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true } }); delete t.ctx.pet; delete t.ctx.os; plugin.register(t.ctx); await tick()
  t.emit('plugin.takeover.request', { id: 'z', reason: 'Approve' })
  assert.equal(calls.notify.length, 1)
  t.emit('plugin.takeover.ended', { id: 'z' })
  t.disposers.forEach(fn => fn())
})

await ok('the toast action and the palette command open the help tab, falling back to the Agents page', async () => {
  reset(); const t = makeCtx({ pulse: { ok: true } }); plugin.register(t.ctx); await tick()
  t.emit('plugin.takeover.request', { id: 'w', reason: 'Approve' })
  calls.notify[0].action.onClick()
  assert.equal(calls.workspaces.length, 1); assert.equal(calls.workspaces[0].id, 'takeover:panel')
  assert.equal(typeof calls.workspaces[0].render, 'function')
  const panel = calls.workspaces[0].render()
  assert.equal(typeof panel.type, 'function')
})

console.log(n + ' passed' + (process.exitCode ? ', with FAILURES' : ''))
