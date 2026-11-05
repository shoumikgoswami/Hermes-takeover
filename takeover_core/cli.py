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


def run(args) -> int:
    cmd = getattr(args, "takeover_cmd", None) or "status"
    try:
        home = compat.current_home()
        if cmd == "setup":
            res = setup.run_setup(home, on_line=lambda l: print("  " + l, flush=True))
            print(f"setup: {res['state']}" + (f" - {res['error']}" if res.get("error") else ""))
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
            print("One-time step in WebUI: Settings -> Extensions -> Diagnostics -> Loopback sidecars -> Takeover -> Approve proxy.")
            print("(WebUI authentication must be on for that; reload the page after approving.)")
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
        if not r["ready"]:
            print("  fix:      hermes takeover setup" + (f"   (or: {r['install_command']})" if r.get("install_command") else ""))
        return 0
    except compat.Incompatible as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
