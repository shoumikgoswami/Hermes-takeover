"""End-to-end against a REAL dashboard, a REAL Bot Desktop (Xvnc+Xfce) and a REAL Chromium, with the plugin installed the
way users install it (`hermes plugins install`). Covers the whole story, including the agent tool running in a different
process from the dashboard.

  HERMES_HOME=<scratch>  hermes plugins install file://<repo> --enable
  HERMES_HOME=<scratch>  HERMES_DASHBOARD_SESSION_TOKEN=e2e-session-token  hermes dashboard --port 9199 --no-open --skip-build
  E2E_HOME=<scratch>  <hermes-venv-python> tests/e2e/run_e2e.py
"""
import json, os, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from cdp import Chrome

HOME = os.environ["E2E_HOME"]
SHOTS = os.environ.get("E2E_SHOTS", tempfile.mkdtemp(prefix="takeover-shots-")); os.makedirs(SHOTS, exist_ok=True)
URL = os.environ.get("E2E_URL", "http://127.0.0.1:9199")
OUT = os.path.join(tempfile.gettempdir(), "takeover-typed.txt")
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

def tid(t): return f'[data-testid="{t}"]'
def text(P, t): return P.eval(f"(document.querySelector('{tid(t)}')||{{}}).textContent||''")
def exists(P, sel): return P.eval(f"!!document.querySelector({sel!r})")

def run_tool(args, results_box):
    """The agent side: the real tool handler in its OWN process, like the gateway would run it."""
    code = f"""
import sys, os, json, importlib.util
sys.path.insert(0, {AGENT_DIR!r}); os.environ['HERMES_HOME']={HOME!r}
root = os.path.join({HOME!r}, 'plugins', 'takeover', 'takeover_core')
spec = importlib.util.spec_from_file_location('hermes_takeover_core', os.path.join(root, '__init__.py'), submodule_search_locations=[root])
m = importlib.util.module_from_spec(spec); sys.modules['hermes_takeover_core'] = m; spec.loader.exec_module(m)
print(m.tool.handle({args!r}))
"""
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=240)
    results_box.append(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "ERR " + p.stderr[-400:])

chrome = Chrome()
term = None
try:
    A = chrome.new_page(f"{URL}/takeover")
    A.wait(f"!!document.querySelector('{tid('state')}')", 30)
    check("Screen tab renders", True)
    A.wait(f"(document.querySelector('{tid('state')}')||{{}}).textContent==='Screen is off'", 15)
    check("screen off on a fresh home: says so + offers Start", exists(A, tid("start")))
    A.screenshot(f"{SHOTS}/1-off.png")

    A.click(tid("start"))
    A.wait(f"(document.querySelector('{tid('state')}')||{{}}).textContent==='Agent is driving'", 60)
    check("Start screen from the page: Bot Desktop comes up", RT.is_running())
    A.wait(f"!!document.querySelector('{tid('take')}')", 30)
    check("live view connects (Take over appears only once connected)", True)
    check("canvas renders the desktop", A.eval("(()=>{const c=document.querySelector('.hdo-screen canvas');return !!c&&c.width>100})()"))

    # ── the stream's own gates, against the dashboard's real WebSocket guard ──
    import urllib.request, urllib.error
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed
    TOKEN = os.environ.get("E2E_TOKEN", "e2e-session-token")
    def http(token):
        h = {"Content-Type": "application/json"}
        if token: h["X-Hermes-Session-Token"] = token
        return urllib.request.Request(f"{URL}/api/plugins/takeover/ticket", data=b"{}", method="POST", headers=h)
    try:
        urllib.request.urlopen(http(None)); unauth = 200
    except urllib.error.HTTPError as e:
        unauth = e.code
    check("control routes refuse a request without the dashboard session token", unauth == 401, unauth)
    def mint(): return json.loads(urllib.request.urlopen(http(TOKEN)).read())["ticket"]
    def ws_try(ticket, origin):
        try:
            with connect(f"ws{URL[4:]}/api/plugins/takeover/stream?ticket={ticket}", origin=origin, subprotocols=["binary"], open_timeout=10) as w:
                w.recv(timeout=5); return "open"
        except ConnectionClosed as e:
            return e.rcvd.code if e.rcvd else None
    tk = mint()
    check("stream opens with a fresh ticket from the page's origin", ws_try(tk, URL) == "open")
    check("a used ticket is refused (4401)", ws_try(tk, URL) == 4401)
    check("a foreign Origin is refused (4403) even with a valid ticket", ws_try(mint(), "http://evil.example") == 4403)
    check("a made-up ticket is refused (4401)", ws_try("nope", URL) == 4401)

    env = dict(l.split("=", 1) for l in open(f"{HOME}/bot-desktop/env").read().splitlines() if "=" in l)
    term = subprocess.Popen(["xfce4-terminal", "--hide-menubar", "-e", f"sh -c 'cat > {OUT}'"], env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    A.screenshot(f"{SHOTS}/2-watching.png")

    # ── the agent asks (tool in another process) ──
    box = []
    t = threading.Thread(target=run_tool, args=({"reason": "Enter the SMS code for the bank login", "wait_seconds": 120}, box))
    t.start()
    A.wait("document.body.innerText.includes('The agent needs you')", 15)
    check("agent's request shows on the Screen tab", "SMS code" in A.eval("document.body.innerText"))
    B = chrome.new_page(f"{URL}/sessions")
    B.wait("!!document.querySelector('.hdo-global')", 15)
    check("...and as a banner on other dashboard pages", "SMS code" in B.eval("document.querySelector('.hdo-global').textContent"))
    B.screenshot(f"{SHOTS}/3-banner-elsewhere.png")
    B.call("Page.close")

    A.click(tid("take"))
    A.wait(f"!!document.querySelector('{tid('tools')}')", 10)
    check("take over: lease -> human, tools shown", L.get().holder == "human" and exists(A, tid("tools")))
    check("lease keeps the agent's ask as the reason", "SMS code" in L.get().reason, L.get().reason)
    try:
        L.assert_agent_may_act(); fenced = False
    except L.HumanHasControl:
        fenced = True
    check("agent is fenced out while the human drives", fenced)
    A.screenshot(f"{SHOTS}/4-driving.png")

    secret = "P@ss w0rd!#{}[]<>~`|\\\"'é"
    A.eval("document.querySelector('.hdo-type').open = true")
    A.eval(f"(()=>{{const t=document.querySelector('{tid('typed')}'); t.value={json.dumps(secret)}; document.querySelector('{tid('typedEnter')}').checked=true; document.querySelector('{tid('typedSend')}').click();}})()")
    time.sleep(4)
    typed = open(OUT, encoding="utf-8", errors="replace").read() if os.path.exists(OUT) else ""
    check("Type text lands in the remote app (symbols, space, accent, Enter)", typed.strip("\n") == secret, repr(typed))
    check("Type text box clears", A.eval(f"document.querySelector('{tid('typed')}').value===''"))
    A.eval("""(()=>{const i=document.querySelector('.hdo-kbd-input'); i.focus();
      for (const ch of 'abc') i.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertText',data:ch,cancelable:true,bubbles:true}));
      i.dispatchEvent(new InputEvent('beforeinput',{inputType:'deleteContentBackward',cancelable:true,bubbles:true}));
      i.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertLineBreak',cancelable:true,bubbles:true}));})()""")
    time.sleep(2)
    check("soft keyboard: letters, Backspace, Enter", open(OUT, encoding="utf-8", errors="replace").read().endswith("\nab\n"))
    A.screenshot(f"{SHOTS}/5-typed.png")

    A.click(tid("back"))
    t.join(60)
    out = json.loads(box[0]) if box and box[0].startswith("{") else {"status": "no result: " + str(box)}
    check("hand back: the waiting tool call returns 'completed' by itself", out.get("status") == "completed", out)
    check("lease is the agent's again; request closed", L.get().holder == "agent" and json.load(open(f"{HOME}/plugin-data/takeover/request.json"))["status"] == "done")
    A.screenshot(f"{SHOTS}/6-handed-back.png")

    # ── second device evicts the first ──
    A.wait(f"!!document.querySelector('{tid('take')}')", 10)
    A.click(tid("take")); A.wait(f"!!document.querySelector('{tid('back')}')", 10)
    C = chrome.new_page(f"{URL}/takeover")
    C.wait(f"!!document.querySelector('{tid('take')}')", 30)
    C.click(tid("take")); C.wait(f"!!document.querySelector('{tid('back')}')", 10)
    A.wait(f"!document.querySelector('{tid('back')}')", 15)
    check("second device takes over: the first drops to watching", exists(A, tid("take")) or "Someone" in A.eval("document.body.innerText"))
    before = open(OUT, encoding="utf-8", errors="replace").read()
    time.sleep(1)
    check("evicted device cannot type", not exists(A, tid("tools")) and open(OUT, encoding="utf-8", errors="replace").read() == before)
    A.screenshot(f"{SHOTS}/7-evicted.png")

    # ── tab closed mid-takeover ──
    C.call("Page.close"); time.sleep(3)
    check("holder's tab closed: agent stays locked out", L.get().holder == "human")
    D = chrome.new_page(f"{URL}/takeover")
    D.wait("document.body.innerText.toLowerCase().includes('force hand-back')", 30)
    check("reopened page offers Force hand-back", True)
    D.eval("window.confirm=()=>true"); D.eval("[...document.querySelectorAll('.hdo-link')].find(b=>/force hand-back/i.test(b.textContent)).click()")
    time.sleep(3)
    check("force hand-back: agent resumes", L.get().holder == "agent")
    D.screenshot(f"{SHOTS}/8-forced.png")
finally:
    chrome.close()
    if term: term.terminate()
    try: RT.stop()
    except Exception: pass

bad = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(bad)}/{len(results)} passed" + (f"  FAILED: {bad}" if bad else "") + f"   screenshots: {SHOTS}")
sys.exit(1 if bad else 0)
