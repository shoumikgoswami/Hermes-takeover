/* Handoff touch layer: drive a remote desktop from a phone.

   noVNC's own touch handling is built for tablets: fit-to-width makes a 1440 px desktop unreadably small and scrolling
   needs a precise two-finger drag. This layer replaces it with a phone-first model and owns the view transform
   (zoom / pan) and all pointer input, so coordinates stay correct at any zoom. noVNC still renders the frame buffer and
   handles the keyboard.

     one finger  drag        scroll the page (content follows your finger, with momentum). Watching only: pans the view.
     one finger  tap         click                       (a ripple shows where)
     one finger  hold + drag press-and-drag (select text, sliders, drag and drop) - a short buzz marks the start
     two fingers pinch/drag  zoom and pan the view
     two fingers tap         right click
     mouse                   click / drag / wheel to scroll / Ctrl+wheel to zoom / right click

   Everything sent is a plain RFB PointerEvent (6 bytes). The server still decides whether this viewer may send input
   (RfbClientFilter + the control lease); this module only avoids sending when the UI knows it may not. */

const BTN = { LEFT: 1, MID: 2, RIGHT: 4, UP: 8, DOWN: 16 };
const TAP_MS = 350, LONG_MS = 450, MOVE_PX = 10;

export const NOTCH_PX = 96;              // remote pixels one wheel notch scrolls in Chrome on Linux (measured end to end: 5 notches = 480 px)

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export class TouchView {
  constructor(o) {
    this.stage = o.stage; this.screen = o.screen;
    this.sendRaw = o.sendRaw || (() => {});
    this.canWrite = o.canWrite || (() => false);
    this.focus = o.focus || (() => {});
    this.onChange = o.onChange || (() => {});
    this.notch = o.notchPx || NOTCH_PX;
    this.fbW = 0; this.fbH = 0; this.s = 1; this.tx = 0; this.ty = 0; this.maxS = 3;
    this.cur = { x: 0, y: 0 };
    this.g = null; this.acc = 0; this.mouseMask = 0; this._raf = 0; this._dead = false;
    this.screen.style.position = 'absolute';
    this.screen.style.inset = 'auto';
    this.screen.style.left = '0'; this.screen.style.top = '0';
    this.screen.style.transformOrigin = '0 0';
    this.stage.style.touchAction = 'none';
    this._h = {};
    const on = (el, type, fn, opts) => { this._h[type + (opts && opts.window ? ':w' : '')] = [el, type, fn, opts]; el.addEventListener(type, fn, opts); };
    const cap = { capture: true, passive: false };
    on(this.stage, 'touchstart', (e) => this._ts(e), cap);
    on(this.stage, 'touchmove', (e) => this._tm(e), cap);
    on(this.stage, 'touchend', (e) => this._te(e), cap);
    on(this.stage, 'touchcancel', (e) => this._te(e, true), cap);
    on(this.stage, 'mousedown', (e) => this._md(e), cap);
    on(window, 'mousemove', (e) => this._mm(e), { capture: true });
    on(window, 'mouseup', (e) => this._mu(e), { capture: true });
    on(this.stage, 'wheel', (e) => this._wheel(e), cap);
    on(this.stage, 'contextmenu', (e) => { if (!this._ui(e)) e.preventDefault(); }, cap);
    this._ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => this._resized()) : null;
    if (this._ro) this._ro.observe(this.stage);
  }

  destroy() {
    this._dead = true;
    cancelAnimationFrame(this._raf);
    clearTimeout(this.g && this.g.timer);
    for (const k of Object.keys(this._h)) { const [el, type, fn, opts] = this._h[k]; el.removeEventListener(type, fn, opts); }
    if (this._ro) this._ro.disconnect();
    this.screen.style.transform = ''; this.screen.style.width = ''; this.screen.style.height = '';
  }

  // ── view ─────────────────────────────────────────────────────────────────
  _sz() { const r = this.stage.getBoundingClientRect(); return { w: r.width, h: r.height, l: r.left, t: r.top }; }
  fitScale() { const z = this._sz(); return this.fbW && z.w ? Math.min(z.w / this.fbW, z.h / this.fbH) : 1; }

  setFb(w, h) {
    if (!w || !h || (w === this.fbW && h === this.fbH)) return;
    const first = !this.fbW;
    this.fbW = w; this.fbH = h;
    this.screen.style.width = w + 'px'; this.screen.style.height = h + 'px';
    if (first) this.comfort(); else this._apply();
  }
  /** Readable default on a phone: wider than "fit", centred, top-aligned. */
  comfort() {
    const z = this._sz(); const fit = this.fitScale();
    this.s = clamp(z.w / 700, fit, 1);
    this.tx = (z.w - this.fbW * this.s) / 2; this.ty = 0;
    this._apply();
  }
  fit() {
    const z = this._sz();
    this.s = this.fitScale();
    this.tx = (z.w - this.fbW * this.s) / 2; this.ty = (z.h - this.fbH * this.s) / 2;
    this._apply();
  }
  zoomBy(f, cx, cy) {
    const z = this._sz();
    cx = cx == null ? z.w / 2 : cx; cy = cy == null ? z.h / 2 : cy;
    const ns = clamp(this.s * f, this.fitScale(), this.maxS);
    const px = (cx - this.tx) / this.s, py = (cy - this.ty) / this.s;     // remote point under the anchor stays put
    this.s = ns; this.tx = cx - px * ns; this.ty = cy - py * ns;
    this._apply();
  }
  /** Pan (without zooming) so the remote point (rx, ry) is at the centre of the stage. */
  reveal(rx, ry) { const z = this._sz(); this.tx = z.w / 2 - rx * this.s; this.ty = z.h / 2 - ry * this.s; this._apply(); }
  zoomIn() { this.zoomBy(1.4); }
  zoomOut() { this.zoomBy(1 / 1.4); }
  state() { return { s: +this.s.toFixed(3), tx: Math.round(this.tx), ty: Math.round(this.ty), fb: [this.fbW, this.fbH], fit: +this.fitScale().toFixed(3) }; }

  _resized() { if (this._dead || !this.fbW) return; this.s = Math.max(this.s, this.fitScale()); this._apply(); }
  _apply() {
    const z = this._sz(); const w = this.fbW * this.s, h = this.fbH * this.s;
    this.tx = w <= z.w ? (z.w - w) / 2 : clamp(this.tx, z.w - w, 0);
    this.ty = h <= z.h ? (z.h - h) / 2 : clamp(this.ty, z.h - h, 0);
    this.screen.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.s})`;
    this.onChange(this.state());
  }
  _pan(dx, dy) { this.tx += dx; this.ty += dy; this._apply(); }

  // ── coordinates / output ─────────────────────────────────────────────────
  _remote(cx, cy) {
    const z = this._sz();
    return { x: clamp(Math.round((cx - z.l - this.tx) / this.s), 0, Math.max(0, this.fbW - 1)),
             y: clamp(Math.round((cy - z.t - this.ty) / this.s), 0, Math.max(0, this.fbH - 1)) };
  }
  _msg(mask, p) { return [5, mask, p.x >> 8, p.x & 255, p.y >> 8, p.y & 255]; }
  _emit(list) {
    if (!list.length || !this.canWrite()) return false;
    this.sendRaw(new Uint8Array([].concat(...list)));
    return true;
  }
  _click(p, button) {
    this.cur = p;
    const ok = this._emit([this._msg(0, p), this._msg(button, p), this._msg(0, p)]);
    this.focus();
    return ok;
  }
  _wheelNotches(n, dirDown, p) {
    n = Math.min(n, 10); const list = [this._msg(this.mouseMask, p)]; this.cur = p;
    for (let i = 0; i < n; i++) { list.push(this._msg(dirDown ? BTN.DOWN : BTN.UP, p)); list.push(this._msg(0, p)); }
    return this._emit(list);
  }
  _scrollBy(dyRemote, p) {         // dyRemote > 0: content should move down (finger dragged down) = wheel up
    this.acc += dyRemote;
    const n = Math.trunc(Math.abs(this.acc) / this.notch);
    if (!n) return;
    const down = this.acc < 0;
    this.acc -= Math.sign(this.acc) * n * this.notch;
    this._wheelNotches(n, down, p);
  }
  _ripple(cx, cy, cls) {
    const z = this._sz(); const d = document.createElement('div');
    d.className = 'hdo-tap' + (cls ? ' ' + cls : '');
    d.style.left = (cx - z.l) + 'px'; d.style.top = (cy - z.t) + 'px';
    this.stage.appendChild(d); setTimeout(() => d.remove(), 450);
  }
  _ui(e) { return !!(e.target && e.target.closest && e.target.closest('[data-hdo-ui]')); }

  // ── touch ────────────────────────────────────────────────────────────────
  _ts(e) {
    if (this._ui(e)) return;
    e.preventDefault(); e.stopPropagation();
    cancelAnimationFrame(this._raf); this._raf = 0;                 // a touch stops momentum
    const ts = e.touches;
    if (ts.length === 1) {
      const t = ts[0];
      const g = this.g = { mode: 'pending', x0: t.clientX, y0: t.clientY, x: t.clientX, y: t.clientY, t0: performance.now(),
                           samples: [{ t: performance.now(), y: t.clientY }], timer: 0 };
      g.timer = setTimeout(() => this._long(), LONG_MS);
    } else if (ts.length === 2) {
      const g = this.g || (this.g = { mode: 'pending', samples: [], t0: performance.now() });
      clearTimeout(g.timer);
      if (g.mode === 'drag') { this._emit([this._msg(0, this.cur)]); stageDrag(this, false); }
      const a = ts[0], b = ts[1];
      Object.assign(g, { mode: 'pinch', t0: performance.now(), d0: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
        mx0: (a.clientX + b.clientX) / 2, my0: (a.clientY + b.clientY) / 2, s0: this.s, tx0: this.tx, ty0: this.ty,
        moved: 0, ratio: 1 });
    } else if (this.g) { clearTimeout(this.g.timer); this.g.mode = 'ignore'; }
  }

  _long() {
    const g = this.g; if (!g || g.mode !== 'pending') return;
    if (!this.canWrite()) return;
    g.mode = 'drag';
    const p = this._remote(g.x, g.y); this.cur = p;
    this._emit([this._msg(0, p), this._msg(BTN.LEFT, p)]);
    stageDrag(this, true);
    this._ripple(g.x, g.y, 'hdo-tap-hold');
    if (navigator.vibrate) { try { navigator.vibrate(15); } catch (_) { /* unsupported */ } }
  }

  _tm(e) {
    if (this._ui(e)) return;
    e.preventDefault(); e.stopPropagation();
    const g = this.g; if (!g) return;
    const ts = e.touches;
    if (g.mode === 'pinch' && ts.length >= 2) {
      const a = ts[0], b = ts[1];
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1;
      const mx = (a.clientX + b.clientX) / 2, my = (a.clientY + b.clientY) / 2;
      const z = this._sz();
      const ns = clamp(g.s0 * d / g.d0, this.fitScale(), this.maxS);
      const px = (g.mx0 - z.l - g.tx0) / g.s0, py = (g.my0 - z.t - g.ty0) / g.s0;
      this.s = ns; this.tx = (mx - z.l) - px * ns; this.ty = (my - z.t) - py * ns;
      g.moved = Math.max(g.moved, Math.hypot(mx - g.mx0, my - g.my0)); g.ratio = d / g.d0;
      this._apply();
      return;
    }
    if (ts.length !== 1 || g.mode === 'ignore' || g.mode === 'pinch') return;
    const t = ts[0]; const dx = t.clientX - g.x, dy = t.clientY - g.y;
    if (g.mode === 'pending') {
      if (Math.hypot(t.clientX - g.x0, t.clientY - g.y0) < MOVE_PX) { g.x = t.clientX; g.y = t.clientY; return; }
      clearTimeout(g.timer);
      g.mode = this.canWrite() ? 'scroll' : 'pan';
      g.x = g.x0; g.y = g.y0;                       // start from where the finger went down: no dead zone feeling
      return this._tm(e);
    }
    g.x = t.clientX; g.y = t.clientY;
    const now = performance.now(); g.samples.push({ t: now, y: t.clientY });
    while (g.samples.length > 2 && now - g.samples[0].t > 120) g.samples.shift();
    if (g.mode === 'scroll') {
      this._scrollBy(dy / this.s, this._remote(t.clientX, t.clientY));
    } else if (g.mode === 'pan') {
      this._pan(dx, dy);
    } else if (g.mode === 'drag') {
      const p = this._remote(t.clientX, t.clientY); this.cur = p;
      this._emit([this._msg(BTN.LEFT, p)]);
    }
  }

  _te(e, cancelled) {
    if (this._ui(e)) return;
    e.preventDefault(); e.stopPropagation();
    const g = this.g; if (!g) return;
    if (e.touches.length > 0) { if (g.mode === 'pinch') g.mode = 'ignore'; return; }   // 2 -> 1 finger: wait for all up
    clearTimeout(g.timer); this.g = null;
    const dur = performance.now() - g.t0;
    const ct = e.changedTouches[0];
    const cx = ct ? ct.clientX : g.x, cy = ct ? ct.clientY : g.y;
    if (cancelled) { if (g.mode === 'drag') { this._emit([this._msg(0, this.cur)]); stageDrag(this, false); } return; }
    if (g.mode === 'pending') {
      if (dur < TAP_MS) {
        if (this.canWrite()) { this._ripple(g.x0, g.y0); this._click(this._remote(g.x0, g.y0), BTN.LEFT); }
        else this.focus();
      }
    } else if (g.mode === 'pinch') {
      if (dur < TAP_MS && g.moved < MOVE_PX && Math.abs(g.ratio - 1) < 0.12 && this.canWrite()) {
        this._ripple(g.mx0, g.my0, 'hdo-tap-right'); this._click(this._remote(g.mx0, g.my0), BTN.RIGHT);
      }
    } else if (g.mode === 'drag') {
      const p = this._remote(cx, cy); this.cur = p;
      this._emit([this._msg(BTN.LEFT, p), this._msg(0, p)]); stageDrag(this, false);
    } else if (g.mode === 'scroll') {
      const sm = g.samples; const a = sm[0], b = sm[sm.length - 1];
      const v = sm.length > 1 && b.t > a.t ? (b.y - a.y) / (b.t - a.t) : 0;          // finger px per ms
      if (Math.abs(v) > 0.25) this._fling(v, this._remote(cx, cy));
      else this.acc = 0;
    }
  }

  _fling(v, p) {
    let last = performance.now();
    const step = (now) => {
      if (this._dead) return;
      const dt = Math.min(48, now - last); last = now;
      v *= Math.exp(-dt / 325);
      if (Math.abs(v) < 0.04 || !this.canWrite()) { this.acc = 0; this._raf = 0; return; }
      this._scrollBy((v * dt) / this.s, p);
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  }

  // ── mouse ────────────────────────────────────────────────────────────────
  _md(e) {
    if (this._ui(e)) return;
    e.preventDefault(); e.stopPropagation();
    const bit = e.button === 0 ? BTN.LEFT : e.button === 1 ? BTN.MID : e.button === 2 ? BTN.RIGHT : 0;
    if (!bit) return;
    this.mouseMask |= bit;
    const p = this._remote(e.clientX, e.clientY); this.cur = p;
    this._emit([this._msg(0, p), this._msg(this.mouseMask, p)]);
    this.focus();
  }
  _mm(e) {
    if (!this.mouseMask && !this._over(e)) return;
    const p = this._remote(e.clientX, e.clientY); this.cur = p;
    if (this.canWrite()) this._emit([this._msg(this.mouseMask, p)]);
  }
  _over(e) { const r = this.stage.getBoundingClientRect(); return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom; }
  _mu(e) {
    if (!this.mouseMask) return;
    const bit = e.button === 0 ? BTN.LEFT : e.button === 1 ? BTN.MID : e.button === 2 ? BTN.RIGHT : 0;
    this.mouseMask &= ~bit;
    const p = this._remote(e.clientX, e.clientY); this.cur = p;
    this._emit([this._msg(this.mouseMask, p)]);
  }
  _wheel(e) {
    if (this._ui(e)) return;
    e.preventDefault(); e.stopPropagation();
    if (e.ctrlKey) { const z = this._sz(); this.zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - z.l, e.clientY - z.t); return; }
    const px = e.deltaMode === 1 ? e.deltaY * 40 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
    if (!this.canWrite()) { this._pan(0, -px); return; }
    this.acc += -px;                     // wheel down (deltaY>0) = content moves up = scroll down
    const n = Math.trunc(Math.abs(this.acc) / this.notch);
    if (!n) return;
    const down = this.acc < 0; this.acc -= Math.sign(this.acc) * n * this.notch;
    this._wheelNotches(n, down, this._remote(e.clientX, e.clientY));
  }
}

function stageDrag(tv, on) { tv.stage.classList.toggle('hdo-dragging', on); }
