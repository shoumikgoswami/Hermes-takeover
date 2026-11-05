"""End-to-end against a REAL hermes-webui, a REAL Bot Desktop (Xvnc+Xfce) and a REAL Chromium, with the plugin installed the way
users install it. Everything the browser does goes through WebUI's authenticated sidecar proxy: no WebSocket, extra port or tunnel.

  S=<scratch HERMES_HOME>  W=<scratch WebUI state dir>
  HERMES_HOME=$S hermes plugins install file://<repo> --enable --yes-deps
  (start hermes-webui with HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W HERMES_WEBUI_PASSWORD=e2e-pass-123 HERMES_WEBUI_PORT=8798 ...)
  HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W hermes handoff webui
  E2E_HOME=$S <hermes-venv-python> tests/e2e/run_webui_e2e.py
"""
import json, os, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(__file__))
from cdp import Chrome

HOME = os.environ["E2E_HOME"]
URL = os.environ.get("E2E_URL", "http://127.0.0.1:8798")
PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-pass-123")
SHOTS = os.environ.get("E2E_SHOTS", tempfile.mkdtemp(prefix="handoff-webui-shots-")); os.makedirs(SHOTS, exist_ok=True)
OUT = os.path.join(tempfile.gettempdir(), "handoff-webui-typed.txt")
AGENT_DIR = os.path.expanduser("~/.hermes/hermes-agent")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""), flush=True)


sys.path.insert(0, AGENT_DIR); os.environ["HERMES_HOME"] = HOME
from tools.bot_desktop import lease as L, runtime as RT
L.release()
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
check("the sidecar refuses requests without WebUI's token (503 until WebUI mints it, then 401)", raw("http://127.0.0.1:17901/api/status") in (401, 503))
check("WebUI's proxy refuses an unauthenticated browser (401)", raw(f"{URL}/api/extensions/handoff/sidecar/api/status") == 401)


def js(P, expr): return P.eval(expr)
def state_text(P): return P.eval("(document.querySelector('#mainHandoff [data-f=state]')||{}).textContent||''")
def has(P, sel): return P.eval(f"!!document.querySelector({sel!r}) && !document.querySelector({sel!r}).hidden")
def clk(P, sel): P.eval(f"document.querySelector({sel!r}).click()")


def page(chrome, path="/"):
    P = chrome.new_page("about:blank")
    P.call("Network.enable")
    P.call("Network.setCookie", name=cname, value=cval, url=URL)
    P.call("Page.navigate", url=URL + path)
    P.wait("!!window.__hermesHandoff", 40)
    return P


def run_tool(args, box):
    code = f"""
import sys, os, importlib.util
sys.path.insert(0, {AGENT_DIR!r}); os.environ['HERMES_HOME']={HOME!r}
root = os.path.join({HOME!r}, 'plugins', 'handoff', 'handoff_core')
spec = importlib.util.spec_from_file_location('hermes_handoff_core', os.path.join(root, '__init__.py'), submodule_search_locations=[root])
m = importlib.util.module_from_spec(spec); sys.modules['hermes_handoff_core'] = m; spec.loader.exec_module(m)
print(m.tool.handle({args!r}))
"""
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=240)
    box.append(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "ERR " + p.stderr[-400:])


chrome = Chrome(9334)
term = None
try:
    A = page(chrome)
    # start clean (from inside the page: WebUI's own script attaches the CSRF token): revoke consent left by a previous
    # run and skip WebUI's first-run wizard so it does not cover the page
    A.eval("""Promise.all([
      fetch('/api/extensions/sidecar-proxy-consent',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'handoff',approved:false})}),
      fetch('/api/settings',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({onboarding_completed:true})})]).then(r=>r.map(x=>x.status))""", True)
    A.call("Page.reload"); time.sleep(1)
    A.wait("!!window.__hermesHandoff && !!document.querySelector('.rail .hdo-rail')", 40)
    check("extension loaded by WebUI: Screen rail + phone-nav entries", has(A, ".rail .hdo-rail") and has(A, ".sidebar-nav .hdo-nav"))

    # ── before consent ──
    js(A, "window.__hermesHandoff.open()")
    A.wait("document.querySelector('#mainHandoff [data-f=state]').textContent!=='Connecting…'", 20)
    check("before approval the page explains the one-time step", "approval" in state_text(A).lower(), state_text(A))
    A.screenshot(f"{SHOTS}/1-needs-approval.png")

    # ── the one manual step: consent (what Settings -> Extensions -> Approve does) ──
    r = A.eval("fetch('/api/extensions/sidecar-proxy-consent',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'handoff',approved:true})}).then(r=>r.status)", True)
    check("approving the proxy succeeds (WebUI auth on, token-v1)", r == 200, r)

    check("after consent the sidecar refuses a wrong token (401)", raw("http://127.0.0.1:17901/api/status", headers={"X-Hermes-Sidecar-Token": "nope"}) == 401)
    A.wait("document.querySelector('#mainHandoff [data-f=state]').textContent==='Screen is off'", 30)
    check("after approval: screen off on a fresh home, Start offered", has(A, "#mainHandoff [data-f=start]"))
    A.screenshot(f"{SHOTS}/2-off.png")

    clk(A, "#mainHandoff [data-f=start]")
    A.wait("document.querySelector('#mainHandoff [data-f=state]').textContent==='Agent is driving'", 60)
    check("Start screen from the page: Bot Desktop comes up", RT.is_running())
    A.wait("window.__hermesHandoff.state().connected", 40)
    check("live view connects over the proxied long-poll channel", True)
    check("canvas renders the desktop", A.eval("(()=>{const c=document.querySelector('#mainHandoff canvas');return !!c&&c.width>100})()"))
    check("Take over visible once connected", has(A, "#mainHandoff [data-f=take]"))

    env = dict(l.split("=", 1) for l in open(f"{HOME}/bot-desktop/env").read().splitlines() if "=" in l)
    term = subprocess.Popen(["xfce4-terminal", "--hide-menubar", "-e", f"sh -c 'cat > {OUT}'"], env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    A.screenshot(f"{SHOTS}/3-watching.png")

    # watching: nothing the page sends may reach the desktop, even if the page is poked directly
    A.eval("window.__hermesHandoff.state()")
    key = [4, 1, 0, 0, 0, 0, 0, 0x7a]            # KeyEvent 'z' down
    A.eval(f"(async()=>{{const sid=[...performance.getEntriesByType('resource')].map(e=>e.name).filter(n=>n.includes('/api/rfb/recv')).pop().match(/sid=([^&]+)/)[1]; await fetch('/api/extensions/handoff/sidecar/api/rfb/send?sid='+sid,{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/octet-stream'}},body:new Uint8Array({key})}}); }})()", True)
    time.sleep(1)
    check("a forged key event from a watching page is dropped server-side", not os.path.exists(OUT) or "z" not in open(OUT, errors="replace").read())

    # ── the agent asks (tool in another process) ──
    box = []
    t = threading.Thread(target=run_tool, args=({"reason": "Enter the SMS code for the bank login", "wait_seconds": 150}, box)); t.start()
    A.wait("!document.querySelector('#mainHandoff [data-f=ask]').hidden", 20)
    check("the agent's request shows on the Screen page", "SMS code" in A.eval("document.querySelector('#mainHandoff [data-f=ask]').textContent"))
    A.screenshot(f"{SHOTS}/4-agent-asks.png")

    B = page(chrome)                              # another WebUI page (chat view): the global banner
    B.wait("!!document.querySelector('.hdo-global') && !document.querySelector('.hdo-global').hidden", 25)
    check("...and as a banner on the normal chat page", "SMS code" in B.eval("document.querySelector('.hdo-global').textContent"))
    B.screenshot(f"{SHOTS}/5-banner-on-chat.png")
    B.call("Page.close")

    clk(A, "#mainHandoff [data-f=take]")
    A.wait("window.__hermesHandoff.state().inputOn && !document.querySelector('#mainHandoff [data-f=tools]').hidden", 15)
    check("take over: lease -> human, input on, tools shown", L.get().holder == "human")
    check("the agent's ask stays as the takeover reason", "SMS code" in L.get().reason, L.get().reason)
    try: L.assert_agent_may_act(); fenced = False
    except L.HumanHasControl: fenced = True
    check("agent is fenced out while the human drives", fenced)
    A.screenshot(f"{SHOTS}/6-driving.png")

    secret = "P@ss w0rd!#{}[]<>~`|\\\"'é"
    A.eval("document.querySelector('#mainHandoff .hdo-type').open = true")
    A.eval(f"(()=>{{const t=document.querySelector('#mainHandoff [data-f=typed]'); t.value={json.dumps(secret)}; document.querySelector('#mainHandoff [data-f=typedEnter]').checked=true; document.querySelector('#mainHandoff [data-f=typedSend]').click();}})()")
    time.sleep(6)
    typed = open(OUT, encoding="utf-8", errors="replace").read() if os.path.exists(OUT) else ""
    check("Type text lands in the remote app (symbols, space, accent, Enter)", typed.strip("\n") == secret, repr(typed))
    A.eval("""(()=>{const i=document.querySelector('#mainHandoff [data-f=kbdInput]'); i.focus();
      for (const ch of 'abc') i.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertText',data:ch,cancelable:true,bubbles:true}));
      i.dispatchEvent(new InputEvent('beforeinput',{inputType:'deleteContentBackward',cancelable:true,bubbles:true}));
      i.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertLineBreak',cancelable:true,bubbles:true}));})()""")
    time.sleep(3)
    check("soft keyboard: letters, Backspace, Enter", open(OUT, encoding="utf-8", errors="replace").read().endswith("\nab\n"))
    A.screenshot(f"{SHOTS}/7-typed.png")

    clk(A, "#mainHandoff [data-f=back]")
    t.join(60)
    out = json.loads(box[0]) if box and box[0].startswith("{") else {"status": str(box)}
    check("hand back: the waiting tool call returns 'completed' by itself", out.get("status") == "completed", str(out))
    check("lease is the agent's again", L.get().holder == "agent")

    # ── second device evicts the first ──
    A.wait("!document.querySelector('#mainHandoff [data-f=take]').hidden", 15)
    clk(A, "#mainHandoff [data-f=take]"); A.wait("window.__hermesHandoff.state().mine", 10)
    C = page(chrome); js(C, "window.__hermesHandoff.open()")
    C.wait("window.__hermesHandoff.state().connected", 40)
    clk(C, "#mainHandoff [data-f=take]"); C.wait("window.__hermesHandoff.state().mine", 10)
    A.wait("!window.__hermesHandoff.state().mine", 20)
    check("second device takes over: the first drops to watching", not A.eval("window.__hermesHandoff.state().inputOn"))
    before = open(OUT, encoding="utf-8", errors="replace").read()
    time.sleep(1)
    check("evicted device cannot type", open(OUT, encoding="utf-8", errors="replace").read() == before and A.eval("document.querySelector('#mainHandoff [data-f=tools]').hidden"))
    A.wait("window.__hermesHandoff.state().connected", 20)
    check("evicted device reconnects as a watcher", not A.eval("window.__hermesHandoff.state().inputOn"))
    A.screenshot(f"{SHOTS}/8-evicted.png")

    # ── holder's tab closed mid-takeover ──
    C.call("Page.close"); time.sleep(3)
    check("holder's tab closed: agent stays locked out", L.get().holder == "human")
    D = page(chrome); js(D, "window.__hermesHandoff.open()")
    D.wait("document.querySelector('#mainHandoff [data-f=banner]') && /force hand-back/i.test(document.querySelector('#mainHandoff [data-f=banner]').textContent)", 30)
    check("reopened page offers Force hand-back", True)
    D.eval("window.confirm=()=>true"); D.eval("document.querySelector('#mainHandoff [data-act=force]').click()")
    time.sleep(3)
    check("force hand-back: agent resumes", L.get().holder == "agent")
    D.screenshot(f"{SHOTS}/9-forced.png")

    # ── hiding the page stops the stream; no session leak on the sidecar ──
    js(A, "window.__hermesHandoff.close()")
    time.sleep(8)
    cfg = open(os.path.join(HOME, "config.yaml")).read() if os.path.exists(os.path.join(HOME, "config.yaml")) else ""
    check("first run turned browser.headed on by itself", "headed: true" in cfg, cfg[:200])
    check("leaving the Screen page stops streaming", not A.eval("window.__hermesHandoff.state().connected"))
finally:
    chrome.close()
    if term: term.terminate()
    try: RT.stop()
    except Exception: pass

bad = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(bad)}/{len(results)} passed" + (f"  FAILED: {bad}" if bad else "") + f"   screenshots: {SHOTS}")
sys.exit(1 if bad else 0)
