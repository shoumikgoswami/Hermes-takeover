"""``hermes takeover <status|setup|release|screen>``."""
from __future__ import annotations

import json
import sys

from . import compat, control, setup


def setup_parser(sub) -> None:
    s = sub.add_subparsers(dest="takeover_cmd")
    p = s.add_parser("status", help="Readiness, screen, who holds control, pending request")
    p.add_argument("--json", action="store_true")
    s.add_parser("setup", help="Install what Takeover needs (TigerVNC/Xfce, headed browser); safe to re-run")
    s.add_parser("screen", help="Start the agent's screen now")
    w = s.add_parser("webui", help="hermes-webui: install/refresh the Screen page and its service (or --remove)")
    w.add_argument("--remove", action="store_true", help="uninstall the Screen page from hermes-webui and stop the service")
    s.add_parser("release", help="Force control back to the agent (e.g. after closing a tab mid-takeover)")
    sub.set_defaults(func=run)


def next_step(st) -> str:
    """The one thing the user should do now, in words (`hermes takeover status`)."""
    r, sc, ls, rq, w, su = st["readiness"], st["screen"], st["lease"], st["request"], st["webui"], st.get("setup") or {}
    if not r.get("supported"):
        return "nothing: Takeover needs a Linux host (Bot Desktop)."
    if not r["ready"]:
        if su.get("state") == "running":
            return "wait: the one-time screen setup is running (a minute or two)."
        if su.get("manual_command"):
            return f"run on the server: {su['manual_command']}   then: hermes takeover setup"
        return "run: hermes takeover setup   (or open the Screen page; it sets up by itself)"
    if w.get("detected") and w.get("enabled", True):
        if not w.get("extension") or not w.get("sidecar"):
            return "run: hermes takeover webui   (then reload WebUI)"
        if w.get("consent") is False:
            return "in WebUI, open Screen and tap \"Allow Takeover\" (one time)."
    if ls.get("holder") == "human":
        return "finish the step on the Screen page and tap Hand back   (stuck? hermes takeover release)"
    if rq:
        return "the agent is asking: open Screen and tap Take over."
    if r.get("headed") is False:
        return "run: hermes config set browser.headed true   (the agent's browser is invisible)"
    return "nothing: ready. The agent asks when it needs you."


def run(args) -> int:
    cmd = getattr(args, "takeover_cmd", None) or "status"
    try:
        home = compat.current_home()
        if cmd == "setup":
            res = setup.run_setup(home, on_line=lambda l: print("  " + l, flush=True))
            print(f"setup: {res['state']}" + (f" - {res['error']}" if res.get("error") else ""))
            if res["state"] == "done":
                print("Done. Open the Screen page (hermes-webui: side rail; dashboard: Screen tab). If the agent already had a")
                print("browser open, it shows up there the next time the agent opens one.")
            elif res.get("manual_command"):
                print(f"Run this on the server, then run `hermes takeover setup` again:\n    {res['manual_command']}")
            return 0 if res["state"] == "done" else 1
        if cmd == "screen":
            setup.ensure_ready(home)
            print(json.dumps(control.start_screen(home)))
            return 0
        if cmd == "webui":
            from . import webui
            if getattr(args, "remove", False):
                from .webui import stop_sidecar, uninstall_extension
                stop_sidecar(home)
                print("removed" if uninstall_extension() else "hermes-webui not found")
                return 0
            res = webui.integrate(home, force=True)
            if not res.get("detected"):
                print("hermes-webui not found (looked at $HERMES_WEBUI_STATE_DIR and ~/.hermes/webui).")
                return 1
            sc = res.get("sidecar") or {}
            print(f"Screen page installed in {res['state_dir']}; service {'running' if sc.get('running') else 'NOT running: ' + str(sc.get('error'))}.")
            print("Reload WebUI; the Screen button is in the side rail.")
            if webui.consent_given(webui.state_dir()) is not True:
                print("One-time step: open Screen and tap \"Allow Takeover\", then \"Approve proxy consent\" on the highlighted row")
                print("(WebUI needs a password set for that: Settings -> Password).")
            return 0 if sc.get("running") else 1
        if cmd == "release":
            print(json.dumps(control.force_hand_back(home)["holder"]))
            return 0
        st = control.status(home, None)
        if getattr(args, "json", False):
            print(json.dumps(st, indent=2, default=str))
            return 0
        r, sc, ls, rq = st["readiness"], st["screen"], st["lease"], st["request"]
        print(f"Takeover {st['version']}  (home: {home})")
        print(f"  ready:    {'yes' if r['ready'] else 'NO'}"
              + ("" if r["ready"] else f"  missing={r['missing']} tigervnc={r['xvnc_tigervnc']}"))
        print(f"  headed:   {r['headed']}   screen: {'running' if sc.get('running') else 'off'}")
        print(f"  control:  {ls['holder']}" + (f" ({ls['reason']})" if ls.get("reason") else ""))
        print(f"  request:  {rq['reason'] + ' [' + rq['status'] + ']' if rq else '-'}")
        w = st["webui"]
        print("  webui:    " + (("switched off (webui_integration: false)" if not w.get("enabled", True) else f"extension {w['extension'] or 'not installed'}, service {'up' if w['sidecar'] else 'down'}" + ("" if w.get("serves_this_profile", True) else "  (serving ANOTHER profile: run `hermes takeover webui` to switch it to this one)")) if w["detected"] else "not detected"))
        print("  next:     " + next_step(st))
        return 0
    except compat.Incompatible as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
