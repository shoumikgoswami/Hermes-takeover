/* Takeover: the Screen tab. See the agent's browser live, take over, hand back.
   Plain IIFE against window.__HERMES_PLUGIN_SDK__ (no build step). noVNC 1.6.0 is bundled in ./novnc (no CDN). */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const reg = window.__HERMES_PLUGINS__;
  if (!SDK || !reg) {
    // hermes-webui lists dashboard plugins under Settings -> Plugins and opens them in a sandboxed iframe with no SDK and
    // no backend. The live Screen cannot run there; WebUI gets it from Takeover's own WebUI extension instead. Ask the
    // parent page (where that extension runs) to open it, and say so in case the extension is not installed/approved.
    const host = document.getElementById("pluginPageContainer");
    if (host) {
      const open = () => { try { window.parent.postMessage({ type: "hermes-takeover:open" }, "*"); } catch (_) { /* no parent */ } };
      host.innerHTML = '<div style="font:14px/1.5 system-ui,sans-serif;padding:24px;max-width:520px;color:#ccc">' +
        '<h2 style="margin:0 0 8px;font-size:18px;color:#eee">Takeover</h2>' +
        '<p>Opening the Screen page… In hermes-webui the live screen is the <b>Screen</b> icon in the side rail.</p>' +
        '<p>If nothing happens, approve the Takeover service once: Settings → Extensions → Diagnostics → Loopback sidecars → ' +
        '<b>Takeover</b> → Approve proxy, then reload.</p>' +
        '<button type="button" style="padding:6px 14px;border-radius:999px;border:0;background:#1f6feb;color:#fff;font-weight:600">Open Screen</button></div>';
      host.querySelector("button").addEventListener("click", open);
      open();
    }
    return;
  }
  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useRef } = SDK.hooks;

  const API = "/api/plugins/takeover";
  const BASE = (window.__HERMES_BASE_PATH__ || "").replace(/\/+$/, "");
  const SCRIPT = (document.currentScript && document.currentScript.src) || "";
  const ASSETS = SCRIPT ? new URL(".", SCRIPT).href : BASE + "/dashboard-plugins/takeover/dist/";
  const VIEWER_KEY = "hermes.takeover.viewer";          // sessionStorage: one viewer id per browser tab

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // X11 keysyms
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

  function call(path, body) {
    return SDK.fetchJSON(API + path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) }).catch((e) => ({ ok: false, error: "request_failed", message: String((e && e.message) || e) }));
  }

  function getViewer() { try { return sessionStorage.getItem(VIEWER_KEY) || null; } catch (_) { return null; } }
  function setViewer(v) { try { sessionStorage.setItem(VIEWER_KEY, v); } catch (_) { /* private mode */ } }

  // ── controller: owns the network, the noVNC instance and the timers; React only renders its snapshot ──────
  class Controller {
    constructor() {
      this.s = { status: null, err: null, connected: false, connecting: false, mine: false, inputOn: false,
        note: "", zoom: false, starting: false, active: false };
      this.viewer = getViewer();
      this.listeners = new Set();
      this.RFB = null; this.rfb = null; this.ws = null; this.closeCode = null;
      this.gen = 0; this.retry = 0; this.retried401 = false; this.timer = null; this.reconnectTimer = null;
      this.el = null; this.inflight = false; this.autoSetupAsked = false;
    }
    on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit() { const snap = Object.assign({}, this.s); this.listeners.forEach((fn) => fn(snap)); }
    set(patch) { Object.assign(this.s, patch); this.emit(); }

    // lifecycle ---------------------------------------------------------------------------------------------
    attach(el) { this.el = el; }
    start() { this.set({ active: true }); this.retry = 0; this.retried401 = false; this.schedule(0); }
    stop() {
      this.s.active = false;
      clearTimeout(this.timer); this.timer = null;
      this.disconnect();
    }
    schedule(ms) {
      clearTimeout(this.timer);
      const fast = this.s.connecting || this.s.starting || (this.s.status && this.s.status.setup && this.s.status.setup.state === "running");
      this.timer = setTimeout(() => this.tick(), ms == null ? (fast ? 1000 : 2500) : ms);
    }
    async tick() {
      this.timer = null;
      if (!this.s.active) return;
      if (document.hidden || this.inflight) { this.schedule(); return; }
      this.inflight = true;
      try { await this.refresh(); } finally { this.inflight = false; }
      this.reconcile();
      this.maybeConnect();
      this.schedule();
    }
    async refresh() {
      const r = await call("/status", { viewer_id: this.viewer });
      if (r && r.ok) {
        this.s.status = r; this.s.err = null;
        const st = r.setup || {};
        // First run: ask the backend to set things up once (it applies the auto_install policy itself).
        if ((!r.readiness.ready || r.readiness.headed === false) && r.readiness.supported && !this.autoSetupAsked && st.state !== "running" && !st.attempted) {
          this.autoSetupAsked = true;
          call("/setup", { auto: true });
        }
      } else {
        this.s.err = { error: (r && r.error) || "error", message: (r && r.message) || "" };
      }
      this.emit();
      return r;
    }

    // lease -> local control state --------------------------------------------------------------------------
    reconcile() {
      const lease = this.s.status && this.s.status.lease;
      const mine = !!(lease && lease.mine);
      if (mine && !this.s.mine) { this.set({ mine: true }); this.applyInput(); }
      else if (!mine && this.s.mine) {
        this.set({ mine: false, note: lease && lease.holder === "human" ? "Someone else took over. You are watching." : "Control is back with the agent." });
        this.applyInput();
      }
    }
    async applyInput() {
      if (this.rfb && this.s.mine && this.s.connected && this.rfb.viewOnly) await sleep(350);  // server re-reads the lease every 0.25 s
      if (this.rfb) { this.rfb.viewOnly = !(this.s.mine && this.s.connected); }
      this.set({ inputOn: !!(this.rfb && !this.rfb.viewOnly) });
    }

    // stream ------------------------------------------------------------------------------------------------
    maybeConnect() {
      if (!this.s.active || this.rfb || this.s.connecting || this.reconnectTimer || !this.el) return;
      const st = this.s.status;
      if (!st || this.s.err || !st.screen || !st.screen.running) return;
      this.connect();
    }
    async loadRFB() {
      if (this.RFB) return this.RFB;
      const mod = await import(ASSETS + "novnc/core/rfb.js");
      this.RFB = mod.default;
      return this.RFB;
    }
    wsUrl(j) {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      return proto + "//" + location.host + BASE + (j.stream_path || "/api/plugins/takeover/stream").replace(BASE, "")
        + "?ticket=" + encodeURIComponent(j.ticket);
    }
    async connect() {
      if (this.rfb || this.s.connecting) return;
      const gen = ++this.gen;
      this.set({ connecting: true });
      try {
        const t = await call("/ticket", { viewer_id: this.viewer });
        if (gen !== this.gen) return;
        if (t.viewer_id) { this.viewer = t.viewer_id; setViewer(t.viewer_id); }
        if (!t.ok) { this.set({ connecting: false }); return; }
        const RFB = await this.loadRFB();
        if (gen !== this.gen || !this.el) { this.set({ connecting: false }); return; }
        const ws = new WebSocket(this.wsUrl(t), ["binary"]);
        this.closeCode = null;
        ws.addEventListener("close", (e) => { this.closeCode = e.code; });   // noVNC hides the close code
        const rfb = new RFB(this.el, ws, {});
        rfb.viewOnly = true; rfb.focusOnClick = true; rfb.resizeSession = false; rfb.showDotCursor = false;
        rfb.background = "#000";
        this.rfb = rfb; this.ws = ws;
        this.applyZoom();
        rfb.addEventListener("connect", () => {
          if (gen !== this.gen) return;
          this.retry = 0; this.retried401 = false;
          this.set({ connected: true, connecting: false, note: "" });
          this.applyInput();
          if (/[?&]take=1\b/.test(location.search)) {          // one tap from the banner: take control as soon as it is live
            history.replaceState(null, "", location.pathname);
            const req = this.s.status && this.s.status.request;      // a link alone never pauses the agent
            if (!this.s.mine && req && req.status === "pending") this.takeOver();
          }
        });
        rfb.addEventListener("disconnect", () => setTimeout(() => this.onClosed(gen), 0));
      } catch (e) {
        console.warn("[takeover] connect failed", e);
        this.rfb = null; this.ws = null;
        this.set({ connecting: false });
        this.scheduleReconnect();
      }
    }
    applyZoom() {
      if (!this.rfb) return;
      this.rfb.scaleViewport = !this.s.zoom; this.rfb.clipViewport = this.s.zoom; this.rfb.dragViewport = this.s.zoom;
    }
    setZoom(z) { this.s.zoom = z; this.applyZoom(); this.emit(); }
    disconnect() {
      this.gen++;
      clearTimeout(this.reconnectTimer); this.reconnectTimer = null;
      const rfb = this.rfb;
      this.rfb = null; this.ws = null;
      this.s.connected = false; this.s.connecting = false; this.s.inputOn = false;
      if (rfb) { try { rfb.disconnect(); } catch (_) { /* already closed */ } }
      if (this.el) this.el.textContent = "";
      this.emit();
    }
    scheduleReconnect(ms) {
      if (!this.s.active || this.reconnectTimer) return;
      const wait = ms != null ? ms : Math.min(15000, 1500 * Math.pow(2, this.retry++));
      this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.maybeConnect(); }, wait);
    }
    onClosed(gen) {
      if (gen !== this.gen) return;          // we disconnected on purpose
      const code = this.closeCode;
      this.rfb = null; this.ws = null;
      if (this.el) this.el.textContent = "";
      const patch = { connected: false, connecting: false, inputOn: false };
      if (code === 4000) {                   // another viewer took over
        Object.assign(patch, { mine: false, note: "Someone else took over. You are watching." });
        this.set(patch);
        this.refresh().then(() => { this.reconcile(); });
        this.scheduleReconnect(600);
      } else if (code === 4001) {            // the screen stopped
        Object.assign(patch, { note: "The screen stopped." });
        this.set(patch);
        this.refresh().then(() => this.reconcile());
      } else if (code === 4401) {            // ticket refused: mint a fresh one once
        if (!this.retried401) { this.retried401 = true; this.set(patch); this.scheduleReconnect(200); }
        else { patch.note = "Session expired. Reload the page."; this.set(patch); }
      } else if (code === 4403) {
        patch.note = "The dashboard refused the live view from this address (Host/Origin check). Open the dashboard at the address it was started on.";
        this.set(patch);
      } else {
        this.set(patch);
        this.scheduleReconnect();            // dropped link: keep trying; the banner explains if you still hold control
      }
    }

    // controls ----------------------------------------------------------------------------------------------
    async takeOver() {
      if (!this.viewer) await this.refresh();
      const r = await call("/take-over", { viewer_id: this.viewer });
      if (!r.ok) { this.set({ note: "Take over failed: " + (r.error || r.message || "error") }); return; }
      this.set({ note: "" });
      await this.refresh(); this.reconcile();
      if (this.rfb) this.rfb.focus();
    }
    async handBack() {
      const r = await call("/hand-back", { viewer_id: this.viewer });
      if (!r.ok) { this.set({ note: "Hand back failed: " + (r.error || r.message || "error") }); return; }
      if (this.rfb) this.rfb.viewOnly = true;
      this.set({ mine: false, inputOn: false, note: "Handed back. If the agent asked for this it continues by itself; otherwise tell it you are done." });
      await this.refresh(); this.reconcile();
    }
    async forceHandBack() {
      if (!window.confirm("Force control back to the agent? Whoever is driving now will lose control.")) return;
      await call("/force-hand-back", {});
      this.set({ mine: false });
      await this.refresh(); this.reconcile();
    }
    async startScreen() {
      this.set({ starting: true });
      await call("/start", {});
      const until = Date.now() + 40000;
      while (Date.now() < until && this.s.active) {
        await sleep(1000);
        await this.refresh();
        const st = this.s.status;
        if (st && ((st.screen && st.screen.running) || (st.screen && st.screen.start_error))) break;
      }
      this.set({ starting: false });
      this.maybeConnect();
    }
    async runSetup() { await call("/setup", {}); this.autoSetupAsked = true; this.schedule(0); }
    async dismiss() { await call("/dismiss", {}); await this.refresh(); this.emit(); }

    // phone input ------------------------------------------------------------------------------------------
    press(keysym) { if (this.rfb && this.s.inputOn) this.rfb.sendKey(keysym, null); }
    combo(ch) {
      if (!this.rfb || !this.s.inputOn) return;
      const c = ch.charCodeAt(0);
      this.rfb.sendKey(KS.Control, "ControlLeft", true);
      this.rfb.sendKey(c, null, true); this.rfb.sendKey(c, null, false);
      this.rfb.sendKey(KS.Control, "ControlLeft", false);
    }
    async waitForInput(ms) {            // input unlocks ~350 ms after take-over (the server re-reads the lease)
      const end = Date.now() + ms;
      while (!this.s.inputOn && this.s.mine && Date.now() < end) await sleep(50);
      return this.s.inputOn;
    }
    async typeText(text, enterAfter) {
      await this.waitForInput(3000);
      let skipLF = false;
      for (const ch of text) {
        if (!this.s.inputOn) { this.set({ note: "Typing stopped: you no longer have control." }); return; }
        if (ch === "\r") { skipLF = true; this.press(KS.Enter); await sleep(8); continue; }
        if (ch === "\n" && skipLF) { skipLF = false; continue; }
        skipLF = false;
        this.press(keysymFor(ch));
        await sleep(8);
      }
      if (enterAfter) this.press(KS.Enter);
    }
    async pasteFromPhone() {
      if (!this.rfb || !this.s.inputOn) return;
      let text = "";
      try { text = await navigator.clipboard.readText(); } catch (_) {
        this.set({ note: "The browser blocked clipboard access (it needs HTTPS or localhost). Use \"Type text\" instead." }); return;
      }
      if (!text) return;
      this.rfb.clipboardPasteFrom(text);
      await sleep(120);
      this.combo("v");
    }
  }

  // ── describe the current state in words ────────────────────────────────────────────────────────────────
  function describe(s) {
    if (s.err) {
      if (s.err.error === "incompatible") return { dot: "err", text: "Hermes changed under Takeover", hint: s.err.message };
      return { dot: "err", text: "Cannot reach the Takeover backend", hint: s.err.message || "Is the plugin enabled? Run on the server: hermes plugins enable takeover, then restart hermes dashboard." };
    }
    const st = s.status;
    if (!st) return { dot: "wait", text: "Connecting…" };
    const r = st.readiness, setup = st.setup || {}, scr = st.screen || {}, lease = st.lease || {};
    if (!r.supported) return { dot: "err", text: "Not supported on this host", hint: "Takeover needs a Linux host (Bot Desktop)." };
    if (!r.ready) {
      if (setup.state === "running") return { dot: "wait", text: "Setting up the screen (one time)…", hint: "Installing the screen packages (TigerVNC + Xfce) through Hermes and making the agent's browser visible. This takes a minute or two; this page continues by itself." };
      if (setup.state === "needs_manual") return { dot: "err", text: "One manual step needed", hint: (setup.error || "Setup needs root.") + " After running it, click \"Set up the screen\".", copy: setup.manual_command, retry: true };
      if (setup.state === "failed") return { dot: "err", text: "Setup failed", hint: (setup.error || "") + " Click \"Set up the screen\" to try again, or run on the server: hermes takeover setup", retry: true };
      if (setup.auto_allowed === false) return { dot: "off", text: "One-time setup needed", hint: "Takeover needs the screen packages (TigerVNC + Xfce, installed as root through Hermes) and the agent's browser set to visible. Automatic setup is off, so click \"Set up the screen\", or run on the server: hermes takeover setup", retry: true };
      return { dot: "wait", text: setup.attempted ? "Not set up" : "Starting the one-time setup…", retry: !!setup.attempted };
    }
    if (scr.start_error) return { dot: "err", text: "The screen failed to start", hint: scr.start_error, start: true };
    if (scr.starting || s.starting) return { dot: "wait", text: "Starting the screen…" };
    if (!scr.running) return { dot: "off", text: "Screen is off", hint: "The agent starts it when it needs a browser. You can start it now.", start: true };
    if (lease.holder === "human" && lease.mine) return { dot: "me", text: "You are driving" + (lease.reason ? ": " + lease.reason : ""), idle: "Do the step in this browser, then click Hand back. The agent carries on by itself." };
    if (lease.holder === "human") return { dot: "other", text: "Someone else is driving" };
    const fresh = setup.state === "done" && setup.finished && Date.now() / 1000 - setup.finished < 900;
    return { dot: "agent", text: "Agent is driving",
      idle: fresh ? "Setup finished. If the screen is empty, the agent's browser was opened before setup; it shows up here the next time the agent opens a browser."
        : "You are watching. Click Take over to use the screen yourself; the agent waits until you hand back." };
  }

  async function copyText(text) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {
      try {
        const t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
        document.body.appendChild(t); t.select(); ok = document.execCommand("copy"); t.remove();
      } catch (_) { ok = false; }
    }
    if (!ok) window.prompt("Copy this command and run it on the server:", text);
  }

  function Btn(props) {
    const p = Object.assign({}, props);
    const kids = p.children;
    delete p.children;
    return h("button", Object.assign({ type: "button" }, p, { className: "hdo-btn " + (props.className || "") }), kids);
  }

  // ── the Screen tab ────────────────────────────────────────────────────────────────────────────────────
  function ScreenPage() {
    const ctlRef = useRef(null);
    if (!ctlRef.current) ctlRef.current = new Controller();
    const ctl = ctlRef.current;
    const [s, setS] = useState(Object.assign({}, ctl.s));
    const screenRef = useRef(null);
    const kbdRef = useRef(null);
    const typedRef = useRef(null);
    const enterRef = useRef(null);

    useEffect(() => {
      const off = ctl.on(setS);
      ctl.attach(screenRef.current);
      ctl.start();
      return () => { off(); ctl.stop(); };
    }, []);

    const info = describe(s);
    const st = s.status;
    const scr = (st && st.screen) || {};
    const lease = (st && st.lease) || {};
    const req = st && st.request;
    const ready = !!(st && st.readiness && st.readiness.ready);
    const running = ready && !!scr.running && !s.err;
    const setup = (st && st.setup) || {};

    let banner = null;
    if (s.mine && !s.connected && running) {
      banner = h("div", { className: "hdo-banner hdo-warn" }, "You still have control, but the live view is down. ",
        h("button", { className: "hdo-link", onClick: () => { ctl.disconnect(); ctl.retry = 0; ctl.refresh().then(() => ctl.maybeConnect()); } }, "Reconnect"), " or ",
        h("button", { className: "hdo-link", onClick: () => ctl.forceHandBack() }, "Force hand-back"), ".");
    } else if (info.hint) {
      banner = h("div", { className: "hdo-banner " + (info.dot === "err" ? "hdo-err" : "hdo-info") }, info.hint,
        info.copy ? [" ", h("code", { key: "c", className: "hdo-cmd" }, info.copy), " ",
          h("button", { key: "b", className: "hdo-link", onClick: () => copyText(info.copy) }, "Copy command")] : null);
    } else if (lease.holder === "human" && !lease.mine && running) {
      banner = h("div", { className: "hdo-banner hdo-info" }, "Another device has control. Take over to use this one, or ",
        h("button", { className: "hdo-link", onClick: () => ctl.forceHandBack() }, "force hand-back"), ".");
    } else if (s.note) {
      banner = h("div", { className: "hdo-banner hdo-info" }, s.note);
    } else if (running && st.readiness.headed === false) {
      banner = h("div", { className: "hdo-banner hdo-info" }, "The agent's browser may be invisible (headless), so there may be nothing to see. Run on the server: ",
        h("code", { className: "hdo-cmd" }, "hermes config set browser.headed true"));
    } else if (info.idle && !(req && req.status === "pending")) {
      banner = h("div", { className: "hdo-banner hdo-info" }, info.idle);
    }

    const ask = req && req.status === "pending"
      ? h("div", { className: "hdo-ask" }, h("b", null, "The agent needs you: "), req.reason, " ",
          h("span", { className: "hdo-ask-how" }, s.mine ? "Do it, then click Hand back. " : "Click Take over, do it, then Hand back. "),
          h("button", { className: "hdo-link", onClick: () => ctl.dismiss() }, "Dismiss"))
      : null;

    let empty = "";
    if (!s.connected) {
      if (!st || s.err) empty = "";
      else if (!ready) empty = setup.state === "running" ? "Setting up…" : "Not set up";
      else if (!scr.running) empty = (scr.starting || s.starting) ? "Starting…" : "Screen is off";
      else empty = s.connecting ? "Connecting to the screen…" : "Waiting for the stream…";
    }

    const setupLog = !ready && setup.lines && setup.lines.length
      ? h("pre", { className: "hdo-log" }, setup.lines.slice(-12).join("\n")) : null;

    const onKbd = (e) => {
      const ks = DOM_KEYS[e.key];
      if (ks && !e.nativeEvent.isComposing) { ctl.press(ks); e.preventDefault(); }
    };
    // The soft keyboard: a real <input> receives the OS keyboard; we forward what it produces. beforeinput (not
    // keydown) because Android keyboards send keyCode 229 for everything.
    useEffect(() => {
      const ki = kbdRef.current;
      if (!ki) return undefined;
      const before = (e) => {
        if (e.isComposing) return;
        if (e.inputType === "insertText" && e.data) { for (const ch of e.data) ctl.press(keysymFor(ch)); e.preventDefault(); }
        else if (e.inputType === "deleteContentBackward") { ctl.press(KS.Backspace); e.preventDefault(); }
        else if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") { ctl.press(KS.Enter); e.preventDefault(); }
      };
      const compEnd = (e) => { if (e.data) for (const ch of e.data) ctl.press(keysymFor(ch)); ki.value = ""; };
      ki.addEventListener("beforeinput", before);
      ki.addEventListener("compositionend", compEnd);
      return () => { ki.removeEventListener("beforeinput", before); ki.removeEventListener("compositionend", compEnd); };
    }, []);

    const keyBtn = (label, onClick, extra) => h("button", Object.assign({ type: "button", className: "hdo-key", onClick }, extra || {}), label);

    return h("div", { className: "hdo-page" + (s.mine ? " hdo-driving" : "") },
      h("div", { className: "hdo-head" },
        h("div", { className: "hdo-state" }, h("span", { className: "hdo-dot", "data-s": info.dot, "data-testid": "dot" }),
          h("span", { role: "status", "aria-live": "polite", "data-testid": "state" }, info.text)),
        h("div", { className: "hdo-actions" },
          (info.start || info.retry) && ready ? h(Btn, { onClick: () => ctl.startScreen(), "data-testid": "start" }, "Start screen") : null,
          info.retry && !ready ? h(Btn, { onClick: () => ctl.runSetup(), "data-testid": "setup" }, "Set up the screen") : null,
          running && !s.mine && s.connected ? h(Btn, { className: "hdo-primary", onClick: () => ctl.takeOver(), "data-testid": "take" }, "Take over") : null,
          running && s.mine ? h(Btn, { className: "hdo-primary", onClick: () => ctl.handBack(), "data-testid": "back" }, "Hand back") : null)),
      ask, banner, setupLog,
      h("div", { className: "hdo-stage" },
        h("div", { className: "hdo-screen", ref: screenRef, "data-testid": "screen" }),
        empty ? h("div", { className: "hdo-empty" }, empty) : null),
      running && s.mine && s.connected && s.inputOn ? h("div", { className: "hdo-tools", "data-testid": "tools" },
        h("div", { className: "hdo-row" },
          keyBtn("⌨ Keyboard", () => { kbdRef.current.value = ""; kbdRef.current.focus(); }, { "data-testid": "kbd" }),
          keyBtn("Esc", () => ctl.press(KS.Escape)), keyBtn("Tab", () => ctl.press(KS.Tab)),
          keyBtn("Enter", () => ctl.press(KS.Enter), { "data-testid": "enter" }),
          keyBtn("Ctrl+L", () => ctl.combo("l")), keyBtn("Ctrl+A", () => ctl.combo("a")), keyBtn("Ctrl+V", () => ctl.combo("v")),
          keyBtn("Paste from phone", () => ctl.pasteFromPhone()),
          keyBtn(s.zoom ? "Zoom: 100% (drag to pan)" : "Zoom: fit", () => ctl.setZoom(!s.zoom), { "aria-pressed": String(!!s.zoom) })),
        h("details", { className: "hdo-type" }, h("summary", null, "Type text (passwords, codes)"),
          h("textarea", { ref: typedRef, rows: 2, autoComplete: "off", autoCapitalize: "off", autoCorrect: "off", spellCheck: false,
            "data-testid": "typed", placeholder: "Paste or type here, then Send. Sent as keystrokes; never stored." }),
          h("div", { className: "hdo-row" },
            h("label", { className: "hdo-chk" }, h("input", { type: "checkbox", ref: enterRef, "data-testid": "typedEnter" }), " press Enter after"),
            h(Btn, { "data-testid": "typedSend", onClick: async () => {
              const t = typedRef.current.value; if (!t) return;
              const enter = enterRef.current.checked;
              typedRef.current.value = "";
              await ctl.typeText(t, enter);
            } }, "Send"))),
        h("div", { className: "hdo-hint" }, "Tap = click · two-finger tap = right click · hold, then drag = drag · two-finger drag = scroll")) : null,
      h("input", { className: "hdo-kbd-input", ref: kbdRef, type: "text", inputMode: "text", autoComplete: "off", autoCapitalize: "off",
        autoCorrect: "off", spellCheck: false, tabIndex: -1, "aria-label": "Keyboard input for the remote screen", onKeyDown: onKbd }));
  }

  // ── always-on banner: shows on every dashboard page while the agent is asking, or you hold control ──────────
  function Banner() {
    const [p, setP] = useState(null);
    useEffect(() => {
      let dead = false, timer = null;
      const loop = async () => {
        if (!document.hidden) {
          const r = await call("/pulse", { viewer_id: getViewer() });
          if (!dead && r && r.ok) setP(r);
        }
        if (!dead) timer = setTimeout(loop, 4000);
      };
      loop();
      return () => { dead = true; clearTimeout(timer); };
    }, []);
    if (!p || location.pathname.replace(BASE, "").indexOf("/takeover") === 0) return null;
    const req = p.request, mine = p.lease && p.lease.mine;
    if (!(req && req.status === "pending") && !mine) return null;
    return h("div", { className: "hdo-global" + (mine ? " hdo-global-me" : "") },
      mine ? "You still have control of the agent's screen. " : [h("b", { key: "b" }, "The agent needs you: "), req.reason + " "],
      mine ? h("a", { href: BASE + "/takeover" }, "Open Screen to hand back")
           : [h("a", { key: "t", href: BASE + "/takeover?take=1", className: "hdo-global-take" }, "Take over"), " ",
              h("a", { key: "w", href: BASE + "/takeover" }, "Just watch")]);
  }

  reg.register("takeover", ScreenPage);
  if (reg.registerSlot) reg.registerSlot("takeover", "header-banner", Banner);
})();
