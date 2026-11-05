/* Takeover for hermes-webui: the Screen page. See the agent's browser live, take over, hand back.
   Backend = the plugin's loopback sidecar behind WebUI's authenticated proxy. The live view is noVNC running over a
   long-poll HTTP channel (HttpChannel below) because that proxy cannot carry WebSockets. noVNC 1.6.0 is bundled. */
(() => {
  'use strict';
  if (window.__hermesTakeover) return;

  const PANEL = 'x-takeover';
  const BASE = '/api/extensions/takeover/sidecar';
  const SCRIPT_SRC = (document.currentScript && document.currentScript.src) || '';
  const ASSETS = SCRIPT_SRC ? new URL('.', SCRIPT_SRC).pathname : '/extensions/takeover/assets/';
  const VIEWER_KEY = 'hermesExt:takeover:viewer';       // sessionStorage: one viewer id per browser tab

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isDesktop = () => (typeof _isDesktopWidth === 'function' ? _isDesktopWidth() : window.innerWidth >= 641);
  const sidebarCollapsed = () => (typeof _isSidebarCollapsed === 'function' ? _isSidebarCollapsed() : false);
  function toast(msg, type) {
    if (typeof showToast === 'function') { try { showToast(msg, undefined, type); return; } catch (_) { /* fall through */ } }
    console.log('[takeover] ' + msg);
  }

  const ICON = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/>' +
    '<path d="M8 20h8M12 16v4"/><path d="M9.5 10.5l2 2 3.5-4"/></svg>';

  const KS = { Backspace: 0xff08, Tab: 0xff09, Enter: 0xff0d, Escape: 0xff1b, Left: 0xff51, Up: 0xff52, Right: 0xff53,
    Down: 0xff54, Delete: 0xffff, Home: 0xff50, End: 0xff57, Control: 0xffe3 };
  const DOM_KEYS = { Backspace: KS.Backspace, Tab: KS.Tab, Enter: KS.Enter, Escape: KS.Escape, ArrowLeft: KS.Left,
    ArrowUp: KS.Up, ArrowRight: KS.Right, ArrowDown: KS.Down, Delete: KS.Delete, Home: KS.Home, End: KS.End };
  function keysymFor(ch) {
    const cp = ch.codePointAt(0);
    if (cp === 10 || cp === 13) return KS.Enter;
    if (cp === 9) return KS.Tab;
    if ((cp >= 0x20 && cp <= 0x7e) || (cp >= 0xa0 && cp <= 0xff)) return cp;
    return 0x01000000 + cp;
  }

  // ── state ─────────────────────────────────────────────────────────────────
  const S = {
    active: false, viewer: null, status: null, err: null,
    rfb: null, chan: null, connected: false, connecting: false,
    mine: false, inputOn: false, note: '', zoom: false,
    timer: null, inflight: false, starting: false, gen: 0, retry: 0, retried: false, reconnectTimer: null,
    autoSetupAsked: false, RFB: null,
  };
  try { S.viewer = sessionStorage.getItem(VIEWER_KEY) || null; } catch (_) { /* private mode */ }
  function setViewer(v) { S.viewer = v; try { sessionStorage.setItem(VIEWER_KEY, v); } catch (_) { /* ignore */ } }
  let page = null; let side = null; const F = {};

  // ── API ───────────────────────────────────────────────────────────────────
  async function api(path, body, opts) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const to = setTimeout(() => { try { ctrl && ctrl.abort(); } catch (_) { /* ignore */ } }, (opts && opts.timeout) || 9000);
    try {
      const r = await fetch(BASE + path, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body), signal: ctrl ? ctrl.signal : undefined,
      });
      let json = null;
      try { json = await r.json(); } catch (_) { /* non-JSON: proxy error page */ }
      json = json || {};
      return Object.assign({ ok: r.ok && json.ok !== false, http: r.status }, json, { ok: r.ok && json.ok !== false });
    } catch (e) {
      return { ok: false, http: 0, error: (e && e.name === 'AbortError') ? 'timeout' : 'unreachable' };
    } finally { clearTimeout(to); }
  }

  // ── HttpChannel: a WebSocket-shaped object noVNC can drive, carried over long-polling ──────────────────────────
  class HttpChannel {
    constructor(sid) {
      this.sid = sid; this.binaryType = 'arraybuffer'; this.protocol = ''; this.readyState = 1;
      this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
      this.closeCode = null; this._q = []; this._sending = false; this._fails = 0;
      this._poll();
    }
    _url(p, extra) { return BASE + p + '?sid=' + encodeURIComponent(this.sid) + (extra || ''); }
    send(data) {
      if (this.readyState !== 1) return;
      this._q.push(new Uint8Array(data));         // copy: noVNC reuses its send buffer
      this._flush();
    }
    async _flush() {
      if (this._sending) return;
      this._sending = true;
      try {
        while (this._q.length && this.readyState === 1) {
          let n = 0; const take = [];
          while (this._q.length && n < 256 * 1024) { const c = this._q.shift(); take.push(c); n += c.length; }
          const body = new Uint8Array(n); let o = 0;
          for (const c of take) { body.set(c, o); o += c.length; }
          let r;
          try {
            r = await fetch(this._url('/api/rfb/send'), { method: 'POST', credentials: 'same-origin', cache: 'no-store',
              headers: { 'Content-Type': 'application/octet-stream' }, body });
          } catch (_) { this._fail(1006); return; }
          if (r.status === 410 || r.status === 404) { this._finish(parseInt(r.headers.get('X-Takeover-Closed'), 10) || 4408); return; }
          if (!r.ok) { this._fail(1011); return; }
        }
      } finally { this._sending = false; }
    }
    async _poll() {
      while (this.readyState === 1) {
        let r;
        try {
          r = await fetch(this._url('/api/rfb/recv', '&wait=4'), { credentials: 'same-origin', cache: 'no-store' });
          this._fails = 0;
        } catch (_) {
          if (++this._fails >= 4) { this._fail(1006); return; }
          await sleep(400 * this._fails); continue;
        }
        const closed = parseInt(r.headers.get('X-Takeover-Closed'), 10);
        if (r.status === 404 || r.status === 410) { this._finish(closed || 4408); return; }
        if (!r.ok) {
          if (++this._fails >= 4) { this._fail(r.status === 401 || r.status === 403 ? 4401 : 1011); return; }
          await sleep(400 * this._fails); continue;
        }
        const buf = await r.arrayBuffer();
        if (this.readyState !== 1) return;
        if (buf.byteLength && this.onmessage) this.onmessage({ data: buf });
        if (closed) { this._finish(closed); return; }
      }
    }
    _fail(code) { if (this.onerror) { try { this.onerror({}); } catch (_) { /* ignore */ } } this._finish(code); }
    _finish(code) {
      if (this.readyState === 3) return;
      this.readyState = 3; this.closeCode = code;
      if (this.onclose) { try { this.onclose({ code, wasClean: code === 1000 }); } catch (_) { /* ignore */ } }
    }
    close() {
      if (this.readyState >= 2) return;
      this.readyState = 2;
      fetch(this._url('/api/rfb/close'), { method: 'POST', credentials: 'same-origin', keepalive: true }).catch(() => {});
      this._finish(1000);
    }
  }

  // ── DOM ───────────────────────────────────────────────────────────────────
  function pageHtml() {
    return '<div class="hdo-wrap">' +
      '<div class="hdo-head"><div class="hdo-state"><span class="hdo-dot" data-f="dot"></span><span data-f="state" role="status" aria-live="polite">Connecting…</span></div>' +
      '<div class="hdo-actions"><button type="button" class="hdo-btn" data-f="start" hidden>Start screen</button>' +
      '<button type="button" class="hdo-btn" data-f="setup" hidden>Run setup</button>' +
      '<button type="button" class="hdo-btn hdo-primary" data-f="take" hidden>Take over</button>' +
      '<button type="button" class="hdo-btn hdo-primary" data-f="back" hidden>Hand back</button></div></div>' +
      '<div class="hdo-ask" data-f="ask" hidden></div>' +
      '<div class="hdo-banner" data-f="banner" hidden></div>' +
      '<pre class="hdo-log" data-f="log" hidden></pre>' +
      '<div class="hdo-stage"><div class="hdo-screen" data-f="screen"></div><div class="hdo-empty" data-f="empty"></div></div>' +
      '<div class="hdo-tools" data-f="tools" hidden><div class="hdo-row">' +
      '<button type="button" class="hdo-key" data-f="kbd">⌨ Keyboard</button>' +
      '<button type="button" class="hdo-key" data-k="Escape">Esc</button><button type="button" class="hdo-key" data-k="Tab">Tab</button>' +
      '<button type="button" class="hdo-key" data-k="Enter" data-f="enter">Enter</button>' +
      '<button type="button" class="hdo-key hdo-morebtn" data-f="more" aria-expanded="false">More ▾</button></div>' +
      '<div class="hdo-more" data-f="moreBox" hidden><div class="hdo-row">' +
      '<button type="button" class="hdo-key" data-c="l">Ctrl+L</button><button type="button" class="hdo-key" data-c="a">Ctrl+A</button>' +
      '<button type="button" class="hdo-key" data-c="v">Ctrl+V</button>' +
      '<button type="button" class="hdo-key" data-f="paste">Paste from phone</button></div>' +
      '<div class="hdo-row"><button type="button" class="hdo-key" data-z="out" aria-label="Zoom out">−</button>' +
      '<span class="hdo-zoom" data-f="zoomLabel">100%</span>' +
      '<button type="button" class="hdo-key" data-z="in" aria-label="Zoom in">+</button>' +
      '<button type="button" class="hdo-key" data-z="fit">Fit</button><button type="button" class="hdo-key" data-z="reset">Reset</button></div>' +
      '<details class="hdo-type"><summary>Type text (passwords, codes)</summary>' +
      '<textarea data-f="typed" rows="2" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Paste or type here, then Send. Sent as keystrokes; never stored."></textarea>' +
      '<div class="hdo-row"><label class="hdo-chk"><input type="checkbox" data-f="typedEnter"> press Enter after</label>' +
      '<button type="button" class="hdo-btn" data-f="typedSend">Send</button></div></details>' +
      '<div class="hdo-hint">Drag = scroll · tap = click · hold, then drag = select/drag · pinch = zoom · two-finger tap = right click</div></div></div>' +
      '<input class="hdo-kbd-input" data-f="kbdInput" type="text" inputmode="text" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="Keyboard input for the remote screen" tabindex="-1">' +
      '</div>';
  }

  // hermes-webui opens its mobile drawer on any horizontal drag that starts within 80 px of the screen's left edge, which is
  // exactly where a finger lands when it pans or selects on the remote screen. WebUI exempts "interactive" targets through
  // this global; add the stage to them. Fails safe: if WebUI changes it, we just stop overriding.
  function guardEdgeSwipe() {
    try {
      const orig = window._isInteractiveSwipeTarget;
      if (typeof orig !== 'function' || orig.__hdo) return;
      const wrapped = (t) => { try { if (t && t.closest && t.closest('.hdo-stage')) return true; } catch (_) { /* detached */ } return orig(t); };
      wrapped.__hdo = true; window._isInteractiveSwipeTarget = wrapped;
    } catch (_) { /* non-writable: leave WebUI alone */ }
  }

  function mount() {
    guardEdgeSwipe();
    const main = $('main.main'); const rail = $('.rail');
    if (!main) return false;
    page = document.getElementById('mainTakeover');
    if (!page) {
      page = document.createElement('div');
      page.id = 'mainTakeover'; page.className = 'main-view hdo-page';
      page.setAttribute('role', 'region'); page.setAttribute('aria-label', 'Agent screen');
      page.innerHTML = pageHtml();
      main.appendChild(page);
    }
    $$('[data-f]', page).forEach((el) => { F[el.dataset.f] = el; });
    const sideHost = document.getElementById('panelInsights') || $('.sidebar .panel-view');
    side = $('.panel-view[data-panel-token="takeover"]');
    if (!side && sideHost) {
      side = document.createElement('div');
      side.className = 'panel-view hdo-sidepanel'; side.id = 'panelXTakeover'; side.dataset.panelToken = 'takeover';
      side.innerHTML = '<div class="hdo-side"><div class="hdo-side-title">Screen</div><div class="hdo-side-body">See and take over the agent\'s browser: logins, 2FA, CAPTCHAs, payments.</div></div>';
      sideHost.parentNode.appendChild(side);
    }
    if (rail && !rail.querySelector('[data-panel="' + PANEL + '"]')) {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'rail-btn nav-tab has-tooltip hdo-rail'; btn.dataset.panel = PANEL;
      btn.dataset.tooltip = 'Agent screen'; btn.setAttribute('aria-label', 'Agent screen'); btn.innerHTML = ICON;
      btn.addEventListener('click', (e) => { e.preventDefault(); onNavClick(false); });
      rail.insertBefore(btn, rail.querySelector('.rail-spacer') || null);
    }
    const nav = $('.sidebar-nav');
    if (nav && !nav.querySelector('[data-panel="' + PANEL + '"]')) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'nav-tab has-tooltip has-tooltip--bottom hdo-nav'; b.dataset.panel = PANEL;
      b.dataset.label = 'Screen'; b.dataset.tooltip = 'Agent screen'; b.setAttribute('aria-label', 'Agent screen');
      b.innerHTML = ICON.replace(/width="20" height="20"/, 'width="18" height="18"');
      b.addEventListener('click', (e) => { e.preventDefault(); onNavClick(true); });
      nav.insertBefore(b, nav.querySelector('[data-panel="settings"]') || null);
    }
    bindPage();
    return true;
  }

  // ── navigation (same pattern as core rail pages) ──────────────────────────
  let coreSwitchPanel = null; let navGen = 0;
  async function onNavClick(fromMobileNav) {
    if (S.active && isDesktop() && !fromMobileNav) {
      if (sidebarCollapsed()) { if (typeof expandSidebar === 'function') expandSidebar(); }
      else if (typeof toggleSidebar === 'function') toggleSidebar(true);
      return;
    }
    if (isDesktop() && sidebarCollapsed() && !fromMobileNav && typeof expandSidebar === 'function') expandSidebar();
    await show();
    if (!isDesktop() && typeof closeMobileSidebar === 'function') closeMobileSidebar();
  }
  async function show() {
    const main = $('main.main');
    if (!main || !page) return;
    if (!S.active) {
      const gen = ++navGen;
      const sw = coreSwitchPanel || window.switchPanel;
      if (typeof sw === 'function') {
        let ok = true;
        try { ok = await sw.call(window, 'chat'); } catch (err) { toast('Could not open the screen: ' + ((err && err.message) || err), 'error'); return; }
        if (ok === false) return;
      }
      if (gen !== navGen) return;
    }
    Array.from(main.classList).filter((c) => c.startsWith('showing-')).forEach((c) => main.classList.remove(c));
    main.classList.add('showing-' + PANEL);
    $$('[data-panel]').forEach((t) => t.classList.toggle('active', t.dataset.panel === PANEL));
    $$('.panel-view').forEach((p) => p.classList.remove('active'));
    if (side) side.classList.add('active');
    S.active = true; S.retry = 0; S.retried = false;
    syncTitle(); schedule(0);
  }
  function hide() {
    if (!S.active) return;
    S.active = false;
    const main = $('main.main');
    if (main) main.classList.remove('showing-' + PANEL);
    if (side) side.classList.remove('active');
    $$('[data-panel="' + PANEL + '"]').forEach((t) => t.classList.remove('active'));
    disconnect();                         // stop streaming video to a hidden page (battery, data)
    clearTimeout(S.timer); S.timer = null;
  }
  function syncTitle() {
    if (!S.active) return;
    const title = document.getElementById('appTitlebarTitle'); const sub = document.getElementById('appTitlebarSub');
    if (title && !title.querySelector('input')) { title.textContent = 'Agent screen'; title.ondblclick = null; }
    if (sub) { sub.textContent = ''; sub.hidden = true; }
    if (document.title !== 'Agent screen') document.title = 'Agent screen';
  }
  function wrapCore() {
    const orig = window.switchPanel;
    if (typeof orig === 'function' && !orig.__hdoWrapped) {
      coreSwitchPanel = orig;
      const wrapped = function (name, opts) {
        navGen++; opts = opts || {};
        if (S.active && name !== PANEL) {
          hide();
          const cur = typeof _currentPanel === 'string' ? _currentPanel : null;
          if (opts.fromRailClick && name === cur && isDesktop()) {
            opts = Object.assign({}, opts, { fromRailClick: false });
            if (sidebarCollapsed() && typeof expandSidebar === 'function') expandSidebar();
          }
        }
        return orig.call(this, name, opts);
      };
      wrapped.__hdoWrapped = true; window.switchPanel = wrapped;
    }
    const origNew = window.newSession;
    if (typeof origNew === 'function' && !origNew.__hdoWrapped) {
      const w = function () { navGen++; if (S.active) window.switchPanel('chat'); return origNew.apply(this, arguments); };
      w.__hdoWrapped = true; window.newSession = w;
    }
    const origTitle = window.syncAppTitlebar;
    if (typeof origTitle === 'function' && !origTitle.__hdoWrapped) {
      const t = function () { const r = origTitle.apply(this, arguments); if (S.active) syncTitle(); return r; };
      t.__hdoWrapped = true; window.syncAppTitlebar = t;
    }
  }

  // ── polling ───────────────────────────────────────────────────────────────
  function schedule(ms) {
    clearTimeout(S.timer);
    const setupRunning = S.status && S.status.setup && S.status.setup.state === 'running';
    S.timer = setTimeout(tick, ms == null ? (S.connecting || S.starting || setupRunning ? 1000 : 2500) : ms);
  }
  async function refresh() {
    const r = await api('/api/status', { viewer_id: S.viewer });
    if (r.ok && r.readiness) {
      S.status = r; S.err = null;
      const st = r.setup || {};
      if ((!r.readiness.ready || r.readiness.headed === false) && r.readiness.supported && !S.autoSetupAsked && st.state !== 'running' && !st.attempted) {
        S.autoSetupAsked = true; api('/api/setup', { auto: true });
      }
    } else {
      S.err = { http: r.http, error: r.error || 'error', message: r.message || '' };
    }
    return r;
  }
  async function tick() {
    S.timer = null;
    if (!S.active) return;
    if (document.hidden || S.inflight) { schedule(); return; }
    S.inflight = true;
    try { await refresh(); } finally { S.inflight = false; }
    reconcile(); render(); maybeConnect(); schedule();
  }
  function reconcile() {
    const lease = S.status && S.status.lease;
    const mine = !!(lease && lease.mine);
    if (mine && !S.mine) { S.mine = true; applyInput(); }
    else if (!mine && S.mine) {
      S.mine = false;
      S.note = lease && lease.holder === 'human' ? 'Someone else took over. You are watching.' : 'Control is back with the agent.';
      applyInput();
    }
    $$('.hdo-rail, .hdo-nav').forEach((b) => b.classList.toggle('hdo-held', !!S.mine));
  }
  async function applyInput() {
    if (S.rfb && S.mine && S.connected && S.rfb.viewOnly) await sleep(350);   // the server re-reads the lease every 0.25 s
    if (S.rfb) S.rfb.viewOnly = !(S.mine && S.connected);
    S.inputOn = !!(S.rfb && !S.rfb.viewOnly);
    render();
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  const setHidden = (el, v) => { if (el && el.hidden !== v) el.hidden = v; };
  const setText = (el, t) => { if (el && el.textContent !== t) el.textContent = t; };

  function describe() {
    const e = S.err;
    if (e) {
      if (e.error === 'incompatible') return { dot: 'err', text: 'Hermes changed under Takeover', hint: e.message };
      if (e.http === 401 || e.http === 403) return { dot: 'err', text: 'Waiting for your approval', hint: 'Approve Takeover in Settings → Extensions → Diagnostics → Loopback sidecars → Takeover → Approve proxy. WebUI authentication must be on (Settings → set a password).' };
      if (e.http === 503) return { dot: 'err', text: 'Waiting for your approval', hint: 'Approve Takeover in Settings → Extensions → Diagnostics → Loopback sidecars, then reload.' };
      return { dot: 'err', text: 'Takeover service is not running', hint: 'It starts with your next agent chat. To start it now run: hermes takeover setup', };
    }
    const st = S.status;
    if (!st) return { dot: 'wait', text: 'Connecting…' };
    const r = st.readiness; const setup = st.setup || {}; const scr = st.screen || {}; const lease = st.lease || {};
    if (!r.supported) return { dot: 'err', text: 'Not supported on this host', hint: 'Takeover needs a Linux host (Bot Desktop).' };
    if (!r.ready) {
      if (setup.state === 'running') return { dot: 'wait', text: 'Setting up the screen… (first run)' };
      if (setup.state === 'needs_manual') return { dot: 'err', text: 'One manual step needed', hint: setup.error };
      if (setup.state === 'failed') return { dot: 'err', text: 'Setup failed', hint: setup.error, retry: true };
      return { dot: 'wait', text: 'Preparing…', retry: !!setup.attempted };
    }
    if (scr.start_error) return { dot: 'err', text: 'The screen failed to start', hint: scr.start_error, start: true };
    if (scr.starting || S.starting) return { dot: 'wait', text: 'Starting the screen…' };
    if (!scr.running) return { dot: 'off', text: 'Screen is off', hint: 'The agent starts it when it needs a browser. You can start it now.', start: true };
    if (lease.holder === 'human' && lease.mine) return { dot: 'me', text: 'You are driving' + (lease.reason ? ': ' + lease.reason : '') };
    if (lease.holder === 'human') return { dot: 'other', text: 'Someone else is driving' };
    return { dot: 'agent', text: 'Agent is driving' };
  }

  function render() {
    if (!page) return;
    const info = describe(); const st = S.status;
    const r = (st && st.readiness) || {}; const scr = (st && st.screen) || {}; const lease = (st && st.lease) || {};
    const setup = (st && st.setup) || {}; const req = st && st.request;
    const ready = !!r.ready; const running = ready && !!scr.running && !S.err;
    F.dot.dataset.s = info.dot; setText(F.state, info.text);
    setHidden(F.start, !(st && !S.err && ready && !scr.running && !scr.starting && !S.starting));
    setHidden(F.setup, !(info.retry && !ready));
    setHidden(F.take, !(running && !S.mine && S.connected));
    setHidden(F.back, !(running && S.mine));
    setHidden(F.tools, !(running && S.mine && S.connected && S.inputOn));
    page.classList.toggle('hdo-driving', !!S.mine);

    if (req && req.status === 'pending') {
      F.ask.innerHTML = '';
      const b = document.createElement('b'); b.textContent = 'The agent needs you: ';
      F.ask.appendChild(b); F.ask.appendChild(document.createTextNode(req.reason + ' '));
      const d = document.createElement('button'); d.type = 'button'; d.className = 'hdo-link'; d.textContent = 'Dismiss';
      d.addEventListener('click', async () => { await api('/api/dismiss', {}); await refresh(); render(); });
      F.ask.appendChild(d); setHidden(F.ask, false);
    } else setHidden(F.ask, true);

    let banner = null;
    if (S.mine && !S.connected && running) {
      banner = { cls: 'warn', html: 'You still have control, but the live view is down. <button type="button" class="hdo-link" data-act="reconnect">Reconnect</button> or <button type="button" class="hdo-link" data-act="force">Force hand-back</button>.' };
    } else if (info.hint) {
      banner = { cls: S.err || (st && st.screen && st.screen.start_error) || info.dot === 'err' ? 'err' : 'info', text: info.hint };
    } else if (lease.holder === 'human' && !lease.mine && running) {
      banner = { cls: 'info', html: 'Another device has control. Take over to use this one, or <button type="button" class="hdo-link" data-act="force">force hand-back</button>.' };
    } else if (S.note) {
      banner = { cls: 'info', text: S.note };
    } else if (running && r.headed === false) {
      banner = { cls: 'info', text: 'The agent\'s Chrome may be headless, so there may be nothing to see. Run: hermes config set browser.headed true' };
    }
    if (!banner) setHidden(F.banner, true);
    else {
      F.banner.className = 'hdo-banner hdo-' + banner.cls;
      const key = banner.html || banner.text;
      if (F.banner.dataset.k !== key) { if (banner.html) F.banner.innerHTML = banner.html; else F.banner.textContent = banner.text; F.banner.dataset.k = key; }
      setHidden(F.banner, false);
    }
    const lines = !ready && setup.lines && setup.lines.length ? setup.lines.slice(-12).join('\n') : '';
    setText(F.log, lines); setHidden(F.log, !lines);

    let empty = '';
    if (!S.connected) {
      if (!st || S.err) empty = '';
      else if (!ready) empty = setup.state === 'running' ? 'Setting up…' : 'Not set up';
      else if (!scr.running) empty = (scr.starting || S.starting) ? 'Starting…' : 'Screen is off';
      else empty = S.connecting ? 'Connecting to the screen…' : 'Waiting for the stream…';
    }
    setText(F.empty, empty); setHidden(F.empty, !empty);
  }

  // ── stream ────────────────────────────────────────────────────────────────
  async function loadRFB() {
    if (S.RFB) return S.RFB;
    S.RFB = (await import(ASSETS + 'novnc/core/rfb.js')).default;
    S.TouchView = (await import(ASSETS + 'touch.js')).TouchView;
    return S.RFB;
  }
  function maybeConnect() {
    if (!S.active || S.rfb || S.connecting || S.reconnectTimer) return;
    const st = S.status;
    if (!st || S.err || !st.screen || !st.screen.running || !st.readiness.ready) return;
    connect();
  }
  async function connect() {
    if (S.rfb || S.connecting) return;
    const gen = ++S.gen; S.connecting = true; render();
    try {
      const v = await api('/api/viewer', { viewer_id: S.viewer });
      if (gen !== S.gen) return;
      if (!v.ok) { S.connecting = false; return; }
      setViewer(v.viewer_id);
      const o = await api('/api/rfb/open', { viewer_id: S.viewer });
      if (gen !== S.gen) { if (o.sid) api('/api/rfb/close?sid=' + encodeURIComponent(o.sid), {}); return; }
      if (!o.ok) { S.connecting = false; render(); return; }
      const RFB = await loadRFB();
      if (gen !== S.gen) { api('/api/rfb/close?sid=' + encodeURIComponent(o.sid), {}); return; }
      const chan = new HttpChannel(o.sid);
      const rfb = new RFB(F.screen, chan, {});
      rfb.viewOnly = true; rfb.focusOnClick = true; rfb.resizeSession = false; rfb.showDotCursor = false; rfb.background = '#000';
      rfb.scaleViewport = false; rfb.clipViewport = false; rfb.dragViewport = false;   // the touch layer owns zoom / pan / input
      S.rfb = rfb; S.chan = chan;
      const tv = S.tv = new S.TouchView({
        stage: $('.hdo-stage', page), screen: F.screen,
        sendRaw: (bytes) => { if (S.chan === chan) chan.send(bytes); },
        canWrite: () => !!(S.rfb === rfb && S.inputOn),
        focus: () => { try { rfb.focus(); } catch (_) { /* gone */ } },
        onChange: (st) => { if (F.zoomLabel) F.zoomLabel.textContent = Math.round(st.s * 100) + '%'; },
      });
      const syncFb = () => { const c = F.screen.querySelector('canvas'); if (c && c.width) tv.setFb(c.width, c.height); };
      rfb.addEventListener('desktopsize', syncFb);
      rfb.addEventListener('connect', () => {
        if (gen !== S.gen) return;
        syncFb();
        S.connected = true; S.connecting = false; S.retry = 0; S.retried = false; S.note = '';
        applyInput(); render();
        if (S.autoTake && Date.now() - S.autoTake < 60000) { S.autoTake = 0; takeOver(); }   // the one-tap "Take over"
      });
      rfb.addEventListener('disconnect', () => setTimeout(() => onClosed(gen), 0));
    } catch (e) {
      console.warn('[takeover] connect failed', e);
      S.rfb = null; S.chan = null; S.connecting = false; scheduleReconnect();
    }
    render();
  }
  function disconnect() {
    S.gen++; clearTimeout(S.reconnectTimer); S.reconnectTimer = null;
    const rfb = S.rfb;
    if (S.tv) { S.tv.destroy(); S.tv = null; }
    S.rfb = null; S.chan = null; S.connected = false; S.connecting = false; S.inputOn = false;
    if (rfb) { try { rfb.disconnect(); } catch (_) { /* already closed */ } }
    if (F.screen) F.screen.textContent = '';
  }
  function scheduleReconnect(ms) {
    if (!S.active || S.reconnectTimer) return;
    const wait = ms != null ? ms : Math.min(15000, 1500 * Math.pow(2, S.retry++));
    S.reconnectTimer = setTimeout(() => { S.reconnectTimer = null; maybeConnect(); }, wait);
  }
  function onClosed(gen) {
    if (gen !== S.gen) return;
    const code = S.chan ? S.chan.closeCode : null;
    S.rfb = null; S.chan = null; S.connected = false; S.connecting = false; S.inputOn = false;
    if (F.screen) F.screen.textContent = '';
    if (code === 4000) {                       // another device took over
      S.mine = false; S.note = 'Someone else took over. You are watching.';
      refresh().then(() => { reconcile(); render(); }); scheduleReconnect(600);
    } else if (code === 4001) {                // screen stopped
      S.note = 'The screen stopped.'; refresh().then(() => { reconcile(); render(); });
    } else if (code === 4401) {                // proxy refused: consent revoked / auth expired
      S.note = 'WebUI refused the request. Reload the page; if it persists re-approve Takeover in Settings → Extensions.';
    } else if (code === 1008) {
      S.note = 'The screen sent something the viewer could not understand. Reconnecting.'; scheduleReconnect(800);
    } else {
      scheduleReconnect();                     // idle timeout or dropped link: keep trying
    }
    render();
  }

  // ── controls ──────────────────────────────────────────────────────────────
  async function takeOver() {
    F.take.disabled = true;
    try {
      if (!S.viewer) await refresh();
      const r = await api('/api/take-over', { viewer_id: S.viewer });
      if (!r.ok) { toast('Take over failed: ' + (r.error || r.http), 'error'); return; }
      S.note = ''; await refresh(); reconcile();
      if (S.rfb) S.rfb.focus();
    } finally { F.take.disabled = false; render(); }
  }
  async function handBack() {
    F.back.disabled = true;
    try {
      const r = await api('/api/hand-back', { viewer_id: S.viewer });
      if (!r.ok) { toast('Hand back failed: ' + (r.error || r.http), 'error'); return; }
      if (S.rfb) S.rfb.viewOnly = true;
      S.mine = false; S.inputOn = false;
      S.note = 'Handed back. If the agent asked for this it continues by itself; otherwise tell it you are done.';
      await refresh(); reconcile();
    } finally { F.back.disabled = false; render(); }
  }
  async function forceHandBack() {
    if (!window.confirm('Force control back to the agent? Whoever is driving now will lose control.')) return;
    const r = await api('/api/force-hand-back', {});
    if (!r.ok) { toast('Force hand-back failed: ' + (r.error || r.http), 'error'); return; }
    S.mine = false; await refresh(); reconcile(); render();
  }
  async function startScreen() {
    S.starting = true; render();
    await api('/api/start', {});
    const until = Date.now() + 40000;
    while (Date.now() < until && S.active) {
      await sleep(1000); await refresh();
      const sc = S.status && S.status.screen;
      if (sc && (sc.running || sc.start_error)) break;
    }
    S.starting = false; render(); maybeConnect();
  }

  // ── phone input ───────────────────────────────────────────────────────────
  function press(keysym) { if (S.rfb && S.inputOn) S.rfb.sendKey(keysym, null); }
  function combo(ch) {
    if (!S.rfb || !S.inputOn) return;
    const c = ch.charCodeAt(0);
    S.rfb.sendKey(KS.Control, 'ControlLeft', true); S.rfb.sendKey(c, null, true); S.rfb.sendKey(c, null, false);
    S.rfb.sendKey(KS.Control, 'ControlLeft', false);
  }
  async function typeText(text, enterAfter) {
    const end = Date.now() + 3000;
    while (!S.inputOn && S.mine && Date.now() < end) await sleep(50);
    let skipLF = false;
    for (const ch of text) {
      if (!S.inputOn) { toast('Typing stopped: you no longer have control', 'warning'); return; }
      if (ch === '\r') { skipLF = true; press(KS.Enter); await sleep(8); continue; }
      if (ch === '\n' && skipLF) { skipLF = false; continue; }
      skipLF = false; press(keysymFor(ch)); await sleep(8);
    }
    if (enterAfter) press(KS.Enter);
  }
  async function pasteFromPhone() {
    if (!S.rfb || !S.inputOn) return;
    let text = '';
    try { text = await navigator.clipboard.readText(); } catch (_) { toast('The browser blocked clipboard access (it needs HTTPS). Use "Type text" instead.', 'warning'); return; }
    if (!text) { toast('Clipboard is empty'); return; }
    S.rfb.clipboardPasteFrom(text); await sleep(120); combo('v');
  }
  function bindPage() {
    if (page.dataset.bound) return;
    page.dataset.bound = '1';
    F.start.addEventListener('click', startScreen);
    F.setup.addEventListener('click', async () => { await api('/api/setup', {}); S.autoSetupAsked = true; schedule(0); });
    F.take.addEventListener('click', takeOver);
    F.back.addEventListener('click', handBack);
    F.banner.addEventListener('click', (e) => {
      const a = e.target && e.target.closest && e.target.closest('[data-act]');
      if (!a) return;
      if (a.dataset.act === 'force') forceHandBack();
      if (a.dataset.act === 'reconnect') { disconnect(); S.retry = 0; refresh().then(() => { render(); maybeConnect(); }); }
    });
    $$('[data-z]', page).forEach((b) => b.addEventListener('click', () => {
      const tv = S.tv; if (!tv) return;
      ({ in: () => tv.zoomIn(), out: () => tv.zoomOut(), fit: () => tv.fit(), reset: () => tv.comfort() })[b.dataset.z]();
    }));
    F.more.addEventListener('click', () => {
      const open = F.moreBox.hidden; F.moreBox.hidden = !open;
      F.more.setAttribute('aria-expanded', open ? 'true' : 'false'); F.more.textContent = open ? 'Less ▴' : 'More ▾';
    });
    F.paste.addEventListener('click', pasteFromPhone);
    $$('[data-k]', page).forEach((b) => b.addEventListener('click', () => press(KS[b.dataset.k])));
    $$('[data-c]', page).forEach((b) => b.addEventListener('click', () => combo(b.dataset.c)));
    F.typedSend.addEventListener('click', async () => {
      const text = F.typed.value; if (!text) return;
      F.typedSend.disabled = true; F.typed.value = '';
      try { await typeText(text, F.typedEnter.checked); } finally { F.typedSend.disabled = false; }
    });
    const ki = F.kbdInput;       // soft keyboard: beforeinput, not keydown (Android keyboards send keyCode 229)
    F.kbd.addEventListener('click', () => { ki.value = ''; ki.focus(); });
    ki.addEventListener('keydown', (e) => { const ks = DOM_KEYS[e.key]; if (ks && !e.isComposing) { press(ks); e.preventDefault(); } });
    ki.addEventListener('beforeinput', (e) => {
      if (e.isComposing) return;
      if (e.inputType === 'insertText' && e.data) { for (const ch of e.data) press(keysymFor(ch)); e.preventDefault(); }
      else if (e.inputType === 'deleteContentBackward') { press(KS.Backspace); e.preventDefault(); }
      else if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') { press(KS.Enter); e.preventDefault(); }
    });
    ki.addEventListener('compositionend', (e) => { if (e.data) for (const ch of e.data) press(keysymFor(ch)); ki.value = ''; });
    ki.addEventListener('input', () => { if (ki.value.length > 64) ki.value = ''; });
  }

  // ── always-on banner: the agent is asking / you still hold control, on ANY WebUI page ─────────────────────────
  const G = { el: null, delay: 5000 };
  async function globalTick() {
    if (!document.hidden) {
      const r = await api('/api/pulse', { viewer_id: S.viewer }, { timeout: 6000 });
      if (r.ok && r.lease) {
        G.delay = 5000;
        const req = r.request; const mine = !!r.lease.mine;
        const show = !S.active && ((req && req.status === 'pending') || mine);
        if (show) {
          if (!G.el) {
            G.el = document.createElement('div'); G.el.className = 'hdo-global'; G.el.setAttribute('role', 'status');
            document.body.appendChild(G.el);
          }
          G.el.className = 'hdo-global' + (mine ? ' hdo-global-me' : '');
          G.el.textContent = '';
          const msg = document.createElement('span');
          msg.textContent = mine ? 'You still have control of the agent\'s screen. ' : 'The agent needs you: ' + req.reason + ' ';
          const a = document.createElement('button'); a.type = 'button';
          a.className = mine ? 'hdo-link' : 'hdo-global-take'; a.dataset.f = 'globalTake';
          a.textContent = mine ? 'Open Screen to hand back' : 'Take over';
          a.addEventListener('click', () => (mine ? onNavClick(true) : takeOverFromAnywhere()));
          G.el.appendChild(msg); G.el.appendChild(a);
          if (!mine) {
            const w = document.createElement('button'); w.type = 'button'; w.className = 'hdo-link'; w.textContent = 'Just watch';
            w.addEventListener('click', () => onNavClick(true));
            G.el.appendChild(document.createTextNode(' ')); G.el.appendChild(w);
          }
          G.el.hidden = false;
          // sit just below WebUI's titlebar so the menu / title stay usable
          const bar = document.querySelector('.app-titlebar') || (document.getElementById('appTitlebarTitle') || {}).parentElement;
          const bottom = bar && bar.getBoundingClientRect ? bar.getBoundingClientRect().bottom : 0;
          G.el.style.top = Math.max(0, Math.round(bottom) + 6) + 'px';
        } else if (G.el) G.el.hidden = true;
      } else {
        G.delay = 60000;                   // not approved yet / sidecar down: stay quiet and cheap
        if (G.el) G.el.hidden = true;
      }
    }
    setTimeout(globalTick, G.delay);
  }

  /** One tap from any WebUI page (banner, or a link to #takeover): open Screen and take control as soon as it is live. */
  async function takeOverFromAnywhere() {
    S.autoTake = Date.now();
    if (G.el) G.el.hidden = true;
    await onNavClick(true);
    if (S.connected && !S.mine) { S.autoTake = 0; takeOver(); }
  }

  // ── boot ──────────────────────────────────────────────────────────────────
  document.addEventListener('visibilitychange', () => { if (S.active && !document.hidden) schedule(0); });
  window.addEventListener('pagehide', () => { try { if (S.chan) S.chan.close(); } catch (_) { /* ignore */ } });

  function install(attempt) {
    attempt = attempt || 0;
    if ($('main.main') && $('.rail, .sidebar-nav') && typeof window.switchPanel === 'function') {
      mount(); wrapCore();
      window.__hermesTakeover = {
        version: '0.3.0', open: () => onNavClick(true), takeOver: () => takeOverFromAnywhere(), close: () => window.switchPanel('chat'),
        view: () => (S.tv ? S.tv.state() : null), tv: () => S.tv,
        state: () => ({ active: S.active, connected: S.connected, connecting: S.connecting, mine: S.mine, inputOn: S.inputOn,
          err: S.err, closeCode: S.chan ? S.chan.closeCode : null,
          lease: S.status && S.status.lease ? { holder: S.status.lease.holder, mine: S.status.lease.mine } : null }),
      };
      setTimeout(globalTick, 1500);
      if (location.hash === '#takeover') {                 // a link the agent or a notification can point at
        history.replaceState(null, '', location.pathname + location.search);
        takeOverFromAnywhere();
      }
      return;
    }
    if (attempt < 100) setTimeout(() => install(attempt + 1), 150);
    else console.warn('[takeover] WebUI shell not found; not installed');
  }
  window.__hermesTakeover = window.__hermesTakeover || null;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => install(), { once: true });
  else install();
})();
