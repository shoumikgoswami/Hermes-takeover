"""Tiny Chrome DevTools Protocol driver (stdlib + websockets) for the e2e run. No playwright needed."""
import json, os, shutil, subprocess, tempfile, time, urllib.request
from websockets.sync.client import connect

CHROME = os.path.expanduser("~/.hermes/tools/chromium-1208/chrome-linux/chrome")


class Chrome:
    def __init__(self, port=9333):
        self.port = port
        self.dir = tempfile.mkdtemp(prefix="takeover-chrome-")
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--remote-debugging-port={port}", f"--user-data-dir={self.dir}", "--window-size=412,915",
            "--remote-allow-origins=*", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)

    def new_page(self, url):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?{url}", method="PUT")
        t = json.loads(urllib.request.urlopen(req).read())
        return Page(t["webSocketDebuggerUrl"])

    def close(self):
        self.proc.terminate()
        shutil.rmtree(self.dir, ignore_errors=True)


class Page:
    def __init__(self, ws_url):
        self.ws = connect(ws_url, max_size=64 * 1024 * 1024)
        self.n = 0
        self.logs = []
        self.call("Runtime.enable")
        self.call("Page.enable")

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv(timeout=30))
            if m.get("method") == "Runtime.consoleAPICalled":
                self.logs.append(" ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"]))
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def eval(self, expr, await_promise=False):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=await_promise)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r["result"].get("value")

    def wait(self, expr, timeout=15, step=0.2):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = self.eval(expr)
            except RuntimeError:
                v = None
            if v:
                return v
            time.sleep(step)
        raise TimeoutError(f"timed out waiting for: {expr}")

    def click(self, selector):
        self.eval(f"document.querySelector({json.dumps(selector)}).click()")

    def screenshot(self, path):
        import base64
        data = self.call("Page.captureScreenshot", format="png")["data"]
        open(path, "wb").write(base64.b64decode(data))
        return path
