"""End-to-end against a REAL hermes-webui, a REAL Bot Desktop (Xvnc+Xfce) and a REAL Chromium, with the plugin installed the way
users install it. Everything the browser does goes through WebUI's authenticated sidecar proxy: no WebSocket, extra port or tunnel.

  S=<scratch HERMES_HOME>  W=<scratch WebUI state dir>
  HERMES_HOME=$S hermes plugins install file://<repo> --enable --yes-deps
  (start hermes-webui with HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W HERMES_WEBUI_PASSWORD=e2e-pass-123 HERMES_WEBUI_PORT=8798 ...)
  TAKEOVER_PORT=17951 HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W hermes takeover webui
  E2E_HOME=$S E2E_STATE=$W <hermes-venv-python> tests/e2e/run_webui_e2e.py
"""
import json, os, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(__file__))
from cdp import Chrome

HOME = os.environ["E2E_HOME"]
STATE = os.environ["E2E_STATE"]            # the scratch WebUI's state dir
PORT = os.environ.get("TAKEOVER_PORT", "17901")   # use a spare port so a real Takeover service on this host is not disturbed
URL = os.environ.get("E2E_URL", "http://127.0.0.1:8798")
PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-pass-123")
SHOTS = os.environ.get("E2E_SHOTS", tempfile.mkdtemp(prefix="takeover-webui-shots-")); os.makedirs(SHOTS, exist_ok=True)
OUT = os.path.join(tempfile.gettempdir(), "takeover-webui-typed.txt")
AGENT_DIR = os.path.expanduser("~/.hermes/hermes-agent")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""), flush=True)


sys.path.insert(0, AGENT_DIR); os.environ["HERMES_HOME"] = HOME
from tools.bot_desktop import lease as L, runtime as RT
L.release()
for stale in ("setup.json", "request.json"):                       # first-run state is "attempt once": start each run fresh
    try: os.unlink(os.path.join(HOME, "plugin-data", "takeover", stale))
    except OSError: pass
if os.path.exists(OUT): os.remove(OUT)
try: RT.stop()
except Exception: pass

# ── login (cookie) ──
req = urllib.request.Request(f"{URL}/api/auth/login", data=json.dumps({"password": PASSWORD}).encode(), method="POST",
                             headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
cname, cval = cookie.split("=", 1)
check("WebUI login works", bool(cval))

# direct requests that must be refused
def raw(url, method="GET", headers=None, data=None):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers or {}, data=data)).status
    except urllib.error.HTTPError as e:
        return e.code
check("the sidecar refuses requests without WebUI's token (503 until WebUI mints it, then 401)", raw(f"http://127.0.0.1:{PORT}/api/status") in (401, 503))
check("WebUI's proxy refuses an unauthenticated browser (401)", raw(f"{URL}/api/extensions/takeover/sidecar/api/status") == 401)


def js(P, expr): return P.eval(expr)
def state_text(P): return P.eval("(document.querySelector('#mainTakeover [data-f=state]')||{}).textContent||''")
def has(P, sel): return P.eval(f"!!document.querySelector({sel!r}) && !document.querySelector({sel!r}).hidden")
def clk(P, sel): P.eval(f"document.querySelector({sel!r}).click()")


def page(chrome, path="/"):
    P = chrome.new_page("about:blank")
    P.call("Network.enable")
    P.call("Network.setCookie", name=cname, value=cval, url=URL)
    P.call("Page.navigate", url=URL + path)
    P.wait("!!window.__hermesTakeover", 40)
    return P


def run_tool(args, box):
    code = f"""
import sys, os, importlib.util
sys.path.insert(0, {AGENT_DIR!r}); os.environ['HERMES_HOME']={HOME!r}
root = os.path.join({HOME!r}, 'plugins', 'takeover', 'takeover_core')
spec = importlib.util.spec_from_file_location('hermes_takeover_core', os.path.join(root, '__init__.py'), submodule_search_locations=[root])
m = importlib.util.module_from_spec(spec); sys.modules['hermes_takeover_core'] = m; spec.loader.exec_module(m)
print(m.tool.handle({args!r}))
"""
    env = dict(os.environ, HERMES_WEBUI_STATE_DIR=STATE, TAKEOVER_PORT=PORT)       # never let the tool's auto-integration touch the real WebUI
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=240, env=env)
    box.append(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "ERR " + p.stderr[-400:])


chrome = Chrome(9334)
term = None
try:
    A = page(chrome)
    # start clean (from inside the page: WebUI's own script attaches the CSRF token): revoke consent left by a previous
    # run and skip WebUI's first-run wizard so it does not cover the page
    A.eval("""Promise.all([
      fetch('/api/extensions/sidecar-proxy-consent',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'takeover',approved:false})}),
      fetch('/api/settings',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({onboarding_completed:true})})]).then(r=>r.map(x=>x.status))""", True)
    A.call("Page.reload"); time.sleep(1)
    A.wait("!!window.__hermesTakeover && !!document.querySelector('.rail .hdo-rail')", 40)
    check("extension loaded by WebUI: Screen rail + phone-nav entries", has(A, ".rail .hdo-rail") and has(A, ".sidebar-nav .hdo-nav"))

    # ── before consent ──
    js(A, "window.__hermesTakeover.open()")
    A.wait("document.querySelector('#mainTakeover [data-f=state]').textContent!=='Connecting…'", 20)
    check("before approval the page explains the one-time step", "approval" in state_text(A).lower(), state_text(A))
    A.screenshot(f"{SHOTS}/1-needs-approval.png")

    # ── the one manual step: consent (what Settings -> Extensions -> Approve does) ──
    r = A.eval("fetch('/api/extensions/sidecar-proxy-consent',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'takeover',approved:true})}).then(r=>r.status)", True)
    check("approving the proxy succeeds (WebUI auth on, token-v1)", r == 200, r)

    check("after consent the sidecar refuses a wrong token (401)", raw(f"http://127.0.0.1:{PORT}/api/status", headers={"X-Hermes-Sidecar-Token": "nope"}) == 401)
    A.wait("document.querySelector('#mainTakeover [data-f=state]').textContent==='Screen is off'", 30)
    check("after approval: screen off on a fresh home, Start offered", has(A, "#mainTakeover [data-f=start]"))
    A.screenshot(f"{SHOTS}/2-off.png")

    clk(A, "#mainTakeover [data-f=start]")
    A.wait("document.querySelector('#mainTakeover [data-f=state]').textContent==='Agent is driving'", 60)
    check("Start screen from the page: Bot Desktop comes up", RT.is_running())
    A.wait("window.__hermesTakeover.state().connected", 40)
    check("live view connects over the proxied long-poll channel", True)
    check("canvas renders the desktop", A.eval("(()=>{const c=document.querySelector('#mainTakeover canvas');return !!c&&c.width>100})()"))
    check("Take over visible once connected", has(A, "#mainTakeover [data-f=take]"))

    env = dict(l.split("=", 1) for l in open(f"{HOME}/bot-desktop/env").read().splitlines() if "=" in l)

    def active_window():
        try:
            out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], env={**os.environ, **env}, capture_output=True, text=True, timeout=5).stdout
            wid = out.strip().split()[-1]
            return None if wid in ("0x0", "found.") else wid
        except Exception:
            return None
    for _ in range(60):                       # a brand-new desktop: wait until the window manager is managing focus
        if subprocess.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], env={**os.environ, **env}, capture_output=True,
                          text=True).stdout.count("window id") or "0x" in subprocess.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
                          env={**os.environ, **env}, capture_output=True, text=True).stdout:
            break
        A.eval("1"); time.sleep(0.5)          # keep reading the DevTools stream: an unread backlog kills the connection
    before_win = active_window()
    term = subprocess.Popen(["xfce4-terminal", "--hide-menubar", "-e", f"sh -c 'cat > {OUT}'"], env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):                       # ...and until the terminal actually has focus (typed text must land in it)
        w = active_window()
        if w and w != before_win:
            break
        A.eval("1"); time.sleep(0.5)
    time.sleep(1.5)
    A.screenshot(f"{SHOTS}/3-watching.png")

    # watching: nothing the page sends may reach the desktop, even if the page is poked directly
    A.eval("window.__hermesTakeover.state()")
    key = [4, 1, 0, 0, 0, 0, 0, 0x7a]            # KeyEvent 'z' down
    A.eval(f"(async()=>{{const sid=[...performance.getEntriesByType('resource')].map(e=>e.name).filter(n=>n.includes('/api/rfb/recv')).pop().match(/sid=([^&]+)/)[1]; await fetch('/api/extensions/takeover/sidecar/api/rfb/send?sid='+sid,{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/octet-stream'}},body:new Uint8Array({key})}}); }})()", True)
    time.sleep(1)
    check("a forged key event from a watching page is dropped server-side", not os.path.exists(OUT) or "z" not in open(OUT, errors="replace").read())

    # ── the agent asks (tool in another process) ──
    box = []
    t = threading.Thread(target=run_tool, args=({"reason": "Enter the SMS code for the bank login", "wait_seconds": 150}, box)); t.start()
    A.wait("!document.querySelector('#mainTakeover [data-f=ask]').hidden", 20)
    check("the agent's request shows on the Screen page", "SMS code" in A.eval("document.querySelector('#mainTakeover [data-f=ask]').textContent"))
    A.screenshot(f"{SHOTS}/4-agent-asks.png")

    B = page(chrome)                              # another WebUI page (chat view): the global banner
    B.wait("!!document.querySelector('.hdo-global') && !document.querySelector('.hdo-global').hidden", 25)
    check("...and as a banner on the normal chat page", "SMS code" in B.eval("document.querySelector('.hdo-global').textContent"))
    B.screenshot(f"{SHOTS}/5-banner-on-chat.png")
    B.call("Page.close")

    clk(A, "#mainTakeover [data-f=take]")
    A.wait("window.__hermesTakeover.state().inputOn && !document.querySelector('#mainTakeover [data-f=tools]').hidden", 15)
    check("take over: lease -> human, input on, tools shown", L.get().holder == "human")
    check("the agent's ask stays as the takeover reason", "SMS code" in L.get().reason, L.get().reason)
    try: L.assert_agent_may_act(); fenced = False
    except L.HumanHasControl: fenced = True
    check("agent is fenced out while the human drives", fenced)
    A.screenshot(f"{SHOTS}/6-driving.png")

    secret = "P@ss w0rd!#{}[]<>~`|\\\"'é"
    A.eval("document.querySelector('#mainTakeover .hdo-type').open = true")
    A.eval(f"(()=>{{const t=document.querySelector('#mainTakeover [data-f=typed]'); t.value={json.dumps(secret)}; document.querySelector('#mainTakeover [data-f=typedEnter]').checked=true; document.querySelector('#mainTakeover [data-f=typedSend]').click();}})()")
    time.sleep(6)
    typed = open(OUT, encoding="utf-8", errors="replace").read() if os.path.exists(OUT) else ""
    check("Type text lands in the remote app (symbols, space, accent, Enter)", typed.strip("\n") == secret, repr(typed))
    A.eval("""(()=>{const i=document.querySelector('#mainTakeover [data-f=kbdInput]'); i.focus();
      for (const ch of 'abc') i.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertText',data:ch,cancelable:true,bubbles:true}));
      i.dispatchEvent(new InputEvent('beforeinput',{inputType:'deleteContentBackward',cancelable:true,bubbles:true}));
      i.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertLineBreak',cancelable:true,bubbles:true}));})()""")
    time.sleep(3)
    check("soft keyboard: letters, Backspace, Enter", open(OUT, encoding="utf-8", errors="replace").read().endswith("\nab\n"))
    A.screenshot(f"{SHOTS}/7-typed.png")

    # ── phone gestures, against a real headed Chromium running ON the remote desktop ──────────────────────────────
    import urllib.request as _u, base64, shutil
    page_html = os.path.join(tempfile.gettempdir(), "takeover-touch.html")
    open(page_html, "w").write("""<!doctype html><meta charset=utf-8><body style="margin:0;font:22px sans-serif">
<button id=b style="position:fixed;left:40px;top:260px;width:200px;height:90px;font-size:26px" onclick="window.c=(window.c||0)+1;u()">click me</button>
<p id=t style="position:fixed;left:30px;top:120px;width:600px">Select this sentence by holding and dragging across it, please.</p>
<div style="height:30000px;background:linear-gradient(#fff,#ccd)"></div>
<script>window.x=0;function u(){document.title='y='+Math.round(scrollY)+' c='+(window.c||0)+' x='+(window.x||0)}
addEventListener('scroll',u);addEventListener('contextmenu',e=>{window.x++;u();e.preventDefault()});u()</script>""")
    tdir = tempfile.mkdtemp(prefix="takeover-touch-chrome-")
    cenv = {**os.environ, **env}
    cbin = os.environ.get("E2E_CHROME") or shutil.which("chromium") or shutil.which("google-chrome") or \
        os.path.expanduser("~/.hermes/tools/chromium-1208/chrome-linux/chrome")
    rc = subprocess.Popen([cbin, "--no-sandbox", "--password-store=basic", "--no-first-run", "--remote-debugging-port=9444",
                           f"--user-data-dir={tdir}", "--window-position=0,0", "--window-size=1200,760", "--force-device-scale-factor=1",
                           "file://" + page_html], env=cenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def rtab():
        for _ in range(60):
            try:
                return [t for t in json.load(_u.urlopen("http://127.0.0.1:9444/json", timeout=2)) if t["type"] == "page"][0]
            except Exception: time.sleep(0.5)
    from cdp import Page
    R = Page(rtab()["webSocketDebuggerUrl"])
    def title(): return R.eval("document.title")
    def kv(): return dict(x.split("=") for x in title().split())
    def rect(sel):
        return R.eval(f"(()=>{{const r=document.querySelector({sel!r}).getBoundingClientRect();return [r.left,r.top,r.width,r.height,screenX,screenY+outerHeight-innerHeight]}})()")
    time.sleep(3)
    A.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    A.call("Emulation.setDeviceMetricsOverride", width=412, height=860, deviceScaleFactor=2, mobile=True)
    time.sleep(1.5)
    A.wait("window.__hermesTakeover.view() && window.__hermesTakeover.view().fb[0] > 500", 20)
    V = lambda: A.eval("window.__hermesTakeover.view()")
    st0 = V()
    print(f"      phone view: scale {st0['s']} (fit {st0['fit']}), desktop {st0['fb']}")
    check("phone default zoom is more readable than fit-to-width", st0["s"] > st0["fit"] + 0.05 or st0["s"] >= 0.99, str(st0))
    A.eval("document.querySelector('#mainTakeover .hdo-stage').scrollIntoView()")
    nonlocal_stage = [None]
    stage = A.eval("(()=>{const r=document.querySelector('#mainTakeover .hdo-stage').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    print(f"      stage {stage[2]:.0f}x{stage[3]:.0f} css px of a {A.eval('innerHeight')} px tall phone screen")
    def srect():
        A.eval("document.querySelector('#mainTakeover .hdo-stage').scrollIntoView({block:'center'})"); time.sleep(0.3)
        return A.eval("(()=>{const r=document.querySelector('#mainTakeover .hdo-stage').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    def to_client(rx, ry):
        nonlocal_stage[0] = stage = srect()
        A.eval(f"window.__hermesTakeover.tv().reveal({rx}, {ry})"); time.sleep(0.2)       # bring the target into view first
        v = V(); x, y = stage[0] + v["tx"] + rx * v["s"], stage[1] + v["ty"] + ry * v["s"]
        assert stage[0] < x < stage[0] + stage[2] and stage[1] < y < stage[1] + stage[3], ("target outside the stage", x, y, stage)
        return x, y
    def touch(kind, pts):
        A.call("Input.dispatchTouchEvent", type=kind, touchPoints=[{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(pts)])
    def drag(x0, y0, x1, y1, steps=14, dt=0.016, hold=0.0):
        touch("touchStart", [(x0, y0)])
        if hold: time.sleep(hold)
        for i in range(1, steps + 1):
            touch("touchMove", [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)]); time.sleep(dt)
        touch("touchEnd", [])
    def centre():
        r = srect(); return r[0] + r[2] / 2, r[1] + r[3] / 2
    cx, cy = centre()

    # one-finger drag scrolls the remote page, 1:1 with the finger
    cx, cy = centre(); y_before = int(kv()["y"]); s = V()["s"]
    drag(cx, cy + 100, cx, cy - 100, steps=20, dt=0.03)          # slow, no fling
    time.sleep(1.5); moved = int(kv()["y"]) - y_before
    expected = 200 / s
    print(f"      finger moved 200 css px at scale {s}: page scrolled {moved}px (1:1 would be {expected:.0f})")
    check("one-finger drag scrolls the remote page in the right direction", moved > 0, str(moved))
    check("...and tracks the finger (within 25%)", abs(moved - expected) <= 0.25 * expected + 55, f"{moved} vs {expected:.0f}")
    A.screenshot(f"{SHOTS}/8-after-scroll.png")
    drag(cx, cy - 100, cx, cy + 100, steps=20, dt=0.03); time.sleep(1.5)
    check("dragging the other way scrolls back up", int(kv()["y"]) < y_before + moved - 0.5 * expected, title())

    # a quick flick keeps going (momentum)
    cx, cy = centre(); y1 = int(kv()["y"]); drag(cx, cy + 150, cx, cy - 150, steps=6, dt=0.008); time.sleep(2.5)
    flung = int(kv()["y"]) - y1
    check("a quick flick scrolls further than the finger travelled (momentum)", flung > 300 / V()["s"] * 1.3, f"{flung}")

    # tap on the remote button
    bx, by, bw, bh, ox, oy = rect("#b")
    R.eval("window.scrollTo(0,0)"); time.sleep(1)
    bx, by, bw, bh, ox, oy = rect("#b")
    tx_, ty_ = to_client(ox + bx + bw / 2, oy + by + bh / 2)
    c0 = int(kv()["c"]); touch("touchStart", [(tx_, ty_)]); time.sleep(0.05); touch("touchEnd", []); time.sleep(1)
    check("a tap clicks exactly where the finger is (remote button pressed once)", int(kv()["c"]) == c0 + 1, title())

    # two-finger tap = right click
    x0_ = int(kv()["x"]); touch("touchStart", [(tx_ - 30, ty_ + 60), (tx_ + 30, ty_ + 60)]); time.sleep(0.06); touch("touchEnd", []); time.sleep(1)
    check("two-finger tap sends a right click (contextmenu on the remote page)", int(kv()["x"]) == x0_ + 1, title())

    # hold then drag selects text
    R.eval("window.scrollTo(0,0); getSelection().removeAllRanges()"); time.sleep(1)
    px, py, pw, ph, ox, oy = rect("#t")
    a = to_client(ox + px + 4, oy + py + 12); b = to_client(ox + px + 330, oy + py + 12)
    drag(a[0], a[1], b[0], b[1], steps=10, dt=0.03, hold=0.65); time.sleep(1)
    sel = R.eval("getSelection().toString()")
    check("hold-then-drag selects text on the remote page", len(sel) > 10, repr(sel))
    check("a drag that starts at the screen edge does not pop open WebUI's sidebar drawer",
          not A.eval("!!document.querySelector('.sidebar.mobile-open')"))

    # pinch zoom changes the view, and taps stay accurate afterwards
    cx, cy = centre(); z0 = V()["s"]; pass
    touch("touchStart", [(cx - 40, cy), (cx + 40, cy)])
    for i in range(1, 11): touch("touchMove", [(cx - 40 - i * 6, cy), (cx + 40 + i * 6, cy)]); time.sleep(0.016)
    touch("touchEnd", []); time.sleep(0.4)
    z1 = V()["s"]
    check("pinch zooms the view in", z1 > z0 * 1.4, f"{z0} -> {z1}")
    R.eval("window.scrollTo(0,0)"); time.sleep(1)
    bx, by, bw, bh, ox, oy = rect("#b"); tx_, ty_ = to_client(ox + bx + bw / 2, oy + by + bh / 2)
    c0 = int(kv()["c"]); touch("touchStart", [(tx_, ty_)]); time.sleep(0.05); touch("touchEnd", []); time.sleep(1)
    check("taps stay accurate after zooming and panning", int(kv()["c"]) == c0 + 1, title())
    A.screenshot(f"{SHOTS}/9-zoomed.png")
    A.call("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)
    A.call("Emulation.setTouchEmulationEnabled", enabled=False)
    try: R.call("Browser.close")
    except Exception: pass
    rc.terminate()

    clk(A, "#mainTakeover [data-f=back]")
    t.join(60)
    out = json.loads(box[0]) if box and box[0].startswith("{") else {"status": str(box)}
    check("hand back: the waiting tool call returns 'completed' by itself", out.get("status") == "completed", str(out))
    check("lease is the agent's again", L.get().holder == "agent")

    # ── second device evicts the first ──
    A.wait("!document.querySelector('#mainTakeover [data-f=take]').hidden", 15)
    clk(A, "#mainTakeover [data-f=take]"); A.wait("window.__hermesTakeover.state().mine", 10)
    C = page(chrome); js(C, "window.__hermesTakeover.open()")
    C.wait("window.__hermesTakeover.state().connected", 40)
    clk(C, "#mainTakeover [data-f=take]"); C.wait("window.__hermesTakeover.state().mine", 10)
    A.wait("!window.__hermesTakeover.state().mine", 20)
    check("second device takes over: the first drops to watching", not A.eval("window.__hermesTakeover.state().inputOn"))
    before = open(OUT, encoding="utf-8", errors="replace").read()
    time.sleep(1)
    check("evicted device cannot type", open(OUT, encoding="utf-8", errors="replace").read() == before and A.eval("document.querySelector('#mainTakeover [data-f=tools]').hidden"))
    A.wait("window.__hermesTakeover.state().connected", 20)
    check("evicted device reconnects as a watcher", not A.eval("window.__hermesTakeover.state().inputOn"))
    A.screenshot(f"{SHOTS}/8-evicted.png")

    # ── holder's tab closed mid-takeover ──
    C.call("Page.close"); time.sleep(3)
    check("holder's tab closed: agent stays locked out", L.get().holder == "human")
    D = page(chrome); js(D, "window.__hermesTakeover.open()")
    D.wait("document.querySelector('#mainTakeover [data-f=banner]') && /force hand-back/i.test(document.querySelector('#mainTakeover [data-f=banner]').textContent)", 30)
    check("reopened page offers Force hand-back", True)
    D.eval("window.confirm=()=>true"); D.eval("document.querySelector('#mainTakeover [data-act=force]').click()")
    time.sleep(3)
    check("force hand-back: agent resumes", L.get().holder == "agent")
    D.screenshot(f"{SHOTS}/9-forced.png")


    # ── hiding the page stops the stream; no session leak on the sidecar ──
    js(A, "window.__hermesTakeover.close()")
    time.sleep(8)
    cfg = open(os.path.join(HOME, "config.yaml")).read() if os.path.exists(os.path.join(HOME, "config.yaml")) else ""
    check("first run turned browser.headed on by itself", "headed: true" in cfg, cfg[:200])
    check("leaving the Screen page stops streaming", not A.eval("window.__hermesTakeover.state().connected"))

    # ── one tap: the agent asks again, the user taps "Take over" in the banner on the chat page ──
    box2 = []; t2 = threading.Thread(target=run_tool, args=({"reason": "Approve the Hermes app on GitHub", "wait_seconds": 90}, box2)); t2.start()
    E = page(chrome)                                    # an ordinary WebUI page (chat), Screen not open
    E.wait("!!document.querySelector('.hdo-global-take') && !document.querySelector('.hdo-global').hidden", 40)
    check("the banner on the chat page offers a one-tap Take over", "Approve the Hermes app" in E.eval("document.querySelector('.hdo-global').textContent"))
    E.screenshot(f"{SHOTS}/10-one-tap-banner.png")
    E.eval("document.querySelector('.hdo-global-take').click()")
    E.wait("window.__hermesTakeover.state().mine && window.__hermesTakeover.state().inputOn", 40)
    check("one tap opens Screen AND takes control (no second tap)", L.get().holder == "human" and E.eval("window.__hermesTakeover.state().active"))
    E.screenshot(f"{SHOTS}/11-one-tap-driving.png")
    clk(E, "#mainTakeover [data-f=back]"); t2.join(60)
    out2 = json.loads(box2[0]) if box2 and box2[0].startswith("{") else {"status": str(box2)}
    check("...and handing back completes that request", out2.get("status") == "completed", str(out2))
    E.call("Page.close")

    # ── the side panel: explainer, live status, feedback address ──
    F2 = page(chrome)
    F2.wait("!!document.querySelector('#panelXTakeover [data-s=status]') && !/Checking/.test(document.querySelector('#panelXTakeover [data-s=status]').textContent)", 30)
    side_txt = F2.eval("document.querySelector('#panelXTakeover').textContent")
    check("side panel explains it and shows live status", "How it works" in side_txt and "The agent is driving" in side_txt, side_txt[:160])
    check("side panel has the feedback address", F2.eval("document.querySelector('#panelXTakeover a[href^=\"mailto:hello@shoumikgoswami.com\"]') !== null"))
    F2.eval("document.querySelectorAll('.panel-view').forEach(v=>v.classList.remove('active')); document.querySelector('#panelXTakeover').classList.add('active'); document.querySelector('#panelXTakeover').hidden=false")
    F2.eval("(()=>{const sb=document.querySelector('.sidebar'); if (sb) {sb.classList.remove('sidebar-collapsed'); sb.classList.add('mobile-open');}})()")
    time.sleep(0.6); F2.screenshot(f"{SHOTS}/12-side-panel.png")
    F2.call("Page.close")

    # ── Settings -> Plugins: Takeover is listed (WebUI's own Hermes home) and opens the real Screen page ──
    G = page(chrome)
    G.eval("switchPanel('settings')"); time.sleep(1.5); G.eval("switchSettingsSection('plugins')")
    G.wait("document.querySelectorAll('.plugin-card[data-plugin=\"takeover\"]').length > 0", 30)
    cards = G.eval("[...document.querySelectorAll('.plugin-card[data-plugin=\"takeover\"]')].map(c=>c.textContent.replace(/\\s+/g,' ').slice(0,160))")
    check("Settings -> Plugins lists Takeover", len(cards) >= 1, str(cards))
    G.wait("!!document.querySelector('.plugin-card[data-plugin=\"takeover\"] [data-hdo-open]')", 15)
    G.screenshot(f"{SHOTS}/13-plugins-tab.png")
    G.eval("document.querySelector('.plugin-card[data-plugin=\"takeover\"] [data-hdo-open]').click()")
    G.wait("window.__hermesTakeover.state().active", 20)
    check("its Open Screen button opens the live Screen page", G.eval("window.__hermesTakeover.state().active"))
    # the dashboard-tab copy: WebUI opens it in a sandboxed iframe that cannot even load its script while WebUI auth is on
    # (SameSite=Lax session cookie, null origin). Enabling it adds WebUI's own open button; that must go to the live page.
    G.eval("handlePluginEnableToggle('takeover', true).then(()=>1)", True)
    check("dashboard tab enabled through WebUI's own toggle", "takeover" in str(G.eval(
        "fetch('/api/settings',{credentials:'same-origin'}).then(r=>r.json()).then(j=>JSON.stringify(j.dashboard_plugins||{}))", True)))
    js(G, "window.__hermesTakeover.close()"); time.sleep(1)
    G.eval("switchPanel('settings')"); time.sleep(1.5); G.eval("switchSettingsSection('plugins')")
    G.wait("!!document.querySelector('.plugin-card[data-plugin=\"takeover\"] a.plugin-open-btn')", 30)
    G.eval("document.querySelector('.plugin-card[data-plugin=\"takeover\"] a.plugin-open-btn').click()")
    G.wait("window.__hermesTakeover.state().active", 20)
    check("WebUI's own plugin open button lands on the live Screen page, not the sandboxed copy",
          G.eval("window.__hermesTakeover.state().active && !document.querySelector('#pluginPageContainer iframe')"))
    G.call("Page.close")
finally:
    chrome.close()
    if term: term.terminate()
    try: RT.stop()
    except Exception: pass

bad = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(bad)}/{len(results)} passed" + (f"  FAILED: {bad}" if bad else "") + f"   screenshots: {SHOTS}")
sys.exit(1 if bad else 0)
