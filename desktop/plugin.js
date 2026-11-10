// Takeover, Hermes Desktop half. Ships inside the agent plugin (desktop/plugin.js) and is copied to
// $HERMES_HOME/desktop-plugins/takeover/ by Desktop itself; there is nothing to install separately.
//
// What it does: the agent asks you to take over its screen (sign-in, approval, 2FA code, CAPTCHA, payment). The
// plugin's backend runs inside `hermes serve` and emits `plugin.takeover.request` / `plugin.takeover.ended`. Here that
// becomes an in-app alert, a status-bar chip that stays until the request is over, and a native notification when
// Hermes is in the background. Taking over itself happens in Desktop's own Screen pane (the Screen box at the top of
// the bot's pane, or right-click the bot -> Open Screen; then Take over), which shares the same lease the backend
// waits on. When you tap Hand back the agent continues.
//
// Plain ESM, no JSX (Desktop loads disk plugins uncompiled). Only @hermes/plugin-sdk, react, react/jsx-runtime.
import { host, STATUSBAR_AREAS, PALETTE_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'takeover'
const POLL_MS = 15000          // safety net: a window opened after the request was posted never saw the event

const short = (s, n) => {
  s = String(s || '').trim()
  return s.length > n ? s.slice(0, n - 1) + '\u2026' : s
}

// A tiny store (no dependency on SDK atoms) so the logic can be tested without Desktop.
function createStore() {
  let req = null
  const subs = new Set()
  return {
    get: () => req,
    set(next) {
      if (JSON.stringify(next) === JSON.stringify(req)) return false
      req = next
      subs.forEach(fn => {
        try { fn(req) } catch (_) { /* a broken listener must not stop the others */ }
      })
      return true
    },
    subscribe(fn) {
      subs.add(fn)
      return () => subs.delete(fn)
    }
  }
}

// Pure: the next request (or null) after an incoming event or poll result.
function reduce(current, msg) {
  if (!msg) return current
  const p = msg.payload || {}
  if (msg.type === 'request') {
    if (!p.id) return current
    return { id: String(p.id), reason: short(p.reason, 300) || 'The agent needs you.', profile: p.profile || 'default' }
  }
  if (msg.type === 'ended') return current && (!p.id || p.id === current.id) ? null : current
  if (msg.type === 'poll') {                  // a /pulse answer: authoritative for "is something pending right now"
    if (!p.ok) return current                 // backend unreachable: keep what the events told us
    const r = p.request
    if (r && r.status === 'pending') {
      return { id: String(r.id || 'pulse'), reason: short(r.reason, 300) || 'The agent needs you.', profile: current ? current.profile : 'default' }
    }
    return null
  }
  return current
}

export default {
  id: ID,
  name: 'Takeover',
  defaultEnabled: true,
  register(ctx) {
    const store = createStore()
    const seen = new Set()                    // request ids already announced (one alert per request)

    const announce = req => {
      if (!req || seen.has(req.id)) return
      seen.add(req.id)
      try {
        host.notify({
          kind: 'warning',
          title: 'The agent needs you',
          message: req.reason,
          detail: 'Open the bot\u2019s Screen (the Screen box at the top of its pane, or right-click the bot \u2192 Open Screen), click Take over, do the step, then click Hand back.',
          action: { label: 'How', onClick: () => openPanel() }
        })
      } catch (_) { /* toast unavailable */ }
      try { ctx.os.notify({ title: 'The agent needs you', body: req.reason, activate: '/' }) } catch (_) { /* optional, only fires while away */ }
      try { ctx.pet.say('I need you for a moment: ' + short(req.reason, 80), { id: 'takeover', ttlMs: 60000 }) } catch (_) { /* no pet */ }
    }
    const apply = msg => {
      const next = reduce(store.get(), msg)
      if (store.set(next)) announce(next)
    }

    ctx.onEvent('plugin.takeover.request', ev => apply({ type: 'request', payload: ev && ev.payload }))
    ctx.onEvent('plugin.takeover.ended', ev => {
      apply({ type: 'ended', payload: ev && ev.payload })
      try { ctx.pet.clear('takeover') } catch (_) { /* no pet */ }
    })

    const poll = async () => {
      try {
        const r = await ctx.rest('/pulse', { method: 'POST', body: {}, timeoutMs: 6000 })
        apply({ type: 'poll', payload: r })
      } catch (_) { /* backend half not enabled / gateway restarting: events still work */ }
    }
    poll()
    ctx.setInterval(poll, POLL_MS)

    const dismiss = async () => {
      try { await ctx.rest('/dismiss', { method: 'POST', body: {} }) } catch (_) { /* nothing to dismiss */ }
      apply({ type: 'ended', payload: {} })
    }

    function Panel() {
      const [req, setReq] = useState(store.get())
      useEffect(() => store.subscribe(setReq), [])
      const line = (children, className) => jsx('div', { className: className || '', children })
      return jsxs('div', {
        className: 'flex h-full flex-col gap-3 p-4 text-sm',
        children: [
          line(req ? 'The agent needs you' : 'Nothing is waiting for you', 'text-base font-medium'),
          req
            ? line(req.reason, 'whitespace-pre-wrap')
            : line('When the agent hits a sign-in, approval, code, CAPTCHA or payment it will ask here.', 'text-(--ui-text-tertiary)'),
          jsxs('ol', {
            className: 'list-decimal pl-5 text-(--ui-text-tertiary)',
            children: [
              jsx('li', { children: 'Open the bot\u2019s Screen: click the Screen box at the top of its pane, or right-click the bot in the sidebar \u2192 Open Screen.' }),
              jsx('li', { children: 'If it says the screen packages are missing, click Install on host (once). If it says Screen is off, click Start.' }),
              jsx('li', { children: 'Click Take over and do the step in its browser.' }),
              jsx('li', { children: 'Click Hand back. The agent carries on by itself.' })
            ]
          }),
          line('Also on your phone: the Screen page in hermes-webui or the Hermes dashboard does the same.', 'text-xs text-(--ui-text-tertiary)'),
          req ? jsx('div', { className: 'flex gap-2', children: [
            jsx('button', { key: 'd', type: 'button', className: 'rounded-md border px-3 py-1.5 text-(--ui-text-tertiary)', onClick: dismiss, children: 'Dismiss this request' })
          ] }) : null,
          line('Feedback: hello@shoumikgoswami.com', 'mt-auto text-xs text-(--ui-text-tertiary)')
        ]
      })
    }
    function openPanel() {
      if (typeof host.openWorkspace === 'function') host.openWorkspace(ID + ':panel', { title: 'Takeover', render: () => jsx(Panel, {}) })
      else {
        try {
          host.notify({ kind: 'info', title: 'Takeover', message: 'Open the bot\u2019s Screen (right-click the bot \u2192 Open Screen), click Take over, do the step, then Hand back.' })
        } catch (_) { /* no toast */ }
      }
    }

    function Chip() {
      const [req, setReq] = useState(store.get())
      useEffect(() => store.subscribe(setReq), [])
      if (!req) return null
      return jsx('button', {
        type: 'button',
        title: req.reason,
        onClick: openPanel,
        className: 'px-1.5 text-[0.6875rem] font-medium text-(--ui-warning,#d97706)',
        children: 'Agent needs you: ' + short(req.reason, 48)
      })
    }
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 5, render: () => jsx(Chip, {}) })
    ctx.register({
      id: 'palette',
      area: PALETTE_AREA,
      data: { id: ID + '.open', label: 'Takeover: show the agent\u2019s request', keywords: ['takeover', 'screen', 'captcha', 'sign in', 'agent needs'], run: openPanel }
    })
    ctx.onDispose(() => {
      try { ctx.pet.clear('takeover') } catch (_) { /* gone */ }
    })
  }
}
