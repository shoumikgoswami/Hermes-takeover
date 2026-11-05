# Handoff

**Take over your agent's browser, do the one step only you can do, hand it back.**

When the agent hits a login, a 2FA code, a CAPTCHA or a payment page, it calls `request_handoff`. You get a **Screen**
view of the agent's Chrome (works from a phone) in **hermes-webui** and/or the **Hermes dashboard**. Tap **Take over**,
complete the step in that same browser, tap **Hand back**. The agent's waiting tool call returns by itself and it
continues, logged in.

One native Hermes plugin. No tunnel, no reverse proxy, no extra port to open, no config to write.

## Install

```bash
hermes plugins install shoumikgoswami/Hermes-handoff --enable
```

Then just use Hermes. The first agent session wires everything up (below). Remove with `hermes plugins remove handoff`.

| Surface | How it appears | Your one manual step |
|---|---|---|
| **hermes-webui** (the `:8787` app) | A **Screen** icon in the rail / phone nav; an amber banner on every page while the agent is asking | Settings → Extensions → Diagnostics → Loopback sidecars → **Handoff** → **Approve proxy**. That is WebUI's own security consent, and WebUI authentication must be on (Settings → set a password). The Screen page tells you this until you do it. |
| **Hermes dashboard** (`hermes dashboard`) | A **Screen** tab; banner on every page | none (restart the dashboard once if it was already running) |

### What "first run" does (automatically, once)

| Need | How it is handled |
|---|---|
| Screen packages (TigerVNC + Xfce) | Installed through **Hermes's own Bot Desktop installer** (the one the Desktop app's Install button uses). That installer escalates to root, so it works unattended only where root needs no password; otherwise Handoff shows the exact command and a **Run setup** button. |
| TigerVNC specifically | Many images ship a TightVNC `Xvnc`; it passes a "binary exists" check but cannot start Bot Desktop. Handoff detects it and says how to fix it. |
| Agent's Chrome visible | Sets `browser.headed: true` through the config API. |
| hermes-webui (if on this host) | Copies the Screen page into WebUI's extensions folder (loads on the next page render, no WebUI restart) and starts Handoff's small loopback service, detached. The next agent session restarts it if it is not running. |
| Python packages | **None.** Everything is stdlib or already in Hermes; noVNC 1.6.0 is bundled (no CDN). |

Triggers: first agent session, first Screen visit, first `request_handoff`, or `hermes handoff setup` / `hermes handoff webui`.
It runs once automatically; afterwards only on request. Switches (`hermes config set plugins.entries.handoff.settings.<key> false`):
`auto_install` (packages + headed browser) and `webui_integration` (never touch hermes-webui). Linux only (Bot Desktop is
Linux-only); the tool hides itself elsewhere.

## Using it

**Agent side.** `request_handoff(reason, wait_seconds=900)` posts the request and waits until you hand back:

| Result | Meaning |
|---|---|
| `completed` | You did the step and handed back. The agent takes a fresh screenshot and continues. |
| `timeout` / `still_with_human` / `dismissed` / `cancelled` | The agent tells you what it is waiting for and stops. |

While you hold control every screen/browser tool is refused (`HumanHasControl`), even screenshots, because you may be
typing a credential. A bundled skill (`handoff:handoff`) teaches the agent when to ask and never to request secrets in chat.

**Your side.** Open **Screen**. The status line says who is driving; the video is view-only until you **Take over**
(enforced server-side, not just in the UI).

- Phone (hermes-webui): **drag = scroll** (the page follows your finger, with momentum) · tap = click · hold, then drag = select text / drag
  and drop · pinch = zoom and pan · two-finger tap = right click. **More** shows zoom −/+/Fit/Reset, Ctrl+L/A/V and Type text. While you are only
  watching, a drag pans the view. A mouse works too (wheel scrolls, Ctrl+wheel zooms). The stage fills about three quarters of a phone screen.
- The dashboard Screen tab still uses noVNC's own touch handling (two-finger drag = scroll).
- **⌨ Keyboard** opens the phone keyboard. **Type text** sends a pasted string as keystrokes (best for passwords with symbols).
  Esc / Tab / Enter / Ctrl+L / Ctrl+A / Ctrl+V are always there. **Paste from phone** needs HTTPS or localhost.
- Closed the tab mid-takeover? The agent stays locked out until you reopen Screen and press **Force hand-back**
  (or `hermes handoff release`, or `/handoff release`). A second device's Take over evicts the first.

**CLI.** `hermes handoff status | setup | screen | release | webui [--remove]` and `/handoff` in any chat.

## How it works

```
agent process                         hermes-webui (whatever host/port you already use, incl. over Tailscale)
 request_handoff ─┐                    browser ── /api/extensions/handoff/sidecar/* ──► WebUI proxy (your login + CSRF + consent)
 hermes handoff   ├─ handoff_core ─┐                                                     │ adds X-Hermes-Sidecar-Token
 dashboard routes ┘  (lease, setup,│                                                     ▼
                      requests)    ├─ Bot Desktop lease file  ◄── loopback sidecar 127.0.0.1:17901 (stdlib, detached)
hermes dashboard ── /api/plugins/handoff/* ──────────────────────┘     RfbClientFilter + lease gate ──► Xvnc ── agent's Chrome
```

hermes-webui cannot add backend routes and its sidecar proxy cannot carry WebSockets, so on WebUI the live screen is
**noVNC over a long-poll HTTP channel** (a WebSocket-shaped object that noVNC drives): `rfb/open`, `rfb/send` (client→Xvnc),
`rfb/recv` (Xvnc→client, 4 s long-poll, ≤ 400 KiB per response, under WebUI's 512 KiB / 10 s proxy limits). Because every
byte rides WebUI's authenticated proxy, no ticket, extra port or CSP change is needed. The dashboard uses a real WebSocket
(single-use 30 s ticket + the dashboard's Host/Origin check, because HTTP middleware does not cover WebSockets).

## Security model

- WebUI: the sidecar answers only requests carrying the per-extension token that WebUI mints after **your** consent, re-read on
  every request (401 wrong, 503 missing). It binds 127.0.0.1 only. The token protects against other OS users, not against code
  running as you (including the agent).
- Server-side the RFB filter drops key / pointer / clipboard / resize messages unless the lease says *this* viewer holds it;
  noVNC's view-only flag is only UX. Verified by sending forged key events from a watching page.
- The lease is a tool-level fence, not an OS boundary: anything you type is visible to the agent's browser. Don't enter a
  secret you would not trust the agent with.
- Logs never contain tickets, stream session ids, full viewer ids, keystrokes or frames (the sidecar logs no URLs).
- The plugin runs with your user's permissions, like any Hermes plugin. Its only privileged action is the package install,
  delegated to Hermes's Bot Desktop installer; it has no code path of its own that escalates privileges. Its only writes
  outside its own data dir: `browser.headed` in config, and (WebUI) the extension folder + install manifest in WebUI's state dir.
- A WebUI service serves **one Hermes profile** (the lease lives in the profile's home). The first profile to start it wins;
  `hermes handoff webui` from another profile takes it over; `hermes handoff status` says when it is serving a different one.
  Port clash? `HANDOFF_PORT=<n> hermes handoff webui` rewrites the extension to match.

## Requirements and limits

- Hermes with Bot Desktop (`tools.bot_desktop`). The plugin verifies the symbols it uses and says so if a Hermes update changed
  them (`hermes plugins update handoff`). `hermes plugins validate` lists these private imports; Bot Desktop has no public plugin API.
- Dashboard: keep `plugins.isolation: process` (default); the live stream needs the in-process route.
- The agent's browser must be headed and started *after* setup; an already-running headless Chrome shows an empty desktop.
- hermes-webui: authentication must be on (WebUI's rule for token-protected extension backends). Extensions run with full WebUI
  session authority; this one is small, has no third-party scripts, and noVNC is vendored.
- Remote scrolling is sent as mouse-wheel notches (one notch = about 96 px in Chrome); other apps scroll by other amounts per notch, so the
  finger tracking is exact only in Chrome-like apps (constant `NOTCH_PX` in `touch.js`).
- The long-poll channel adds about one HTTP round trip of latency per input; fine for forms, not for games.

## Layout

```
plugin.yaml  __init__.py  sidecar_run.py     tool, CLI, slash command, session hook, bundled skill; detached sidecar entry
handoff_core/   compat (hermes-agent seam) · setup (first run) · control (lease ops) · state · tickets · bridge (dashboard WS)
                sidecar (hermes-webui backend + long-poll stream) · webui (install extension, keep service up) · tool · cli
dashboard/      manifest.json · plugin_api.py (routes + WebSocket) · dist/ (index.js, style.css, novnc/ 1.6.0 vendored)
webui/          manifest.json · assets/handoff.js + handoff.css + touch.js (phone gestures)   (noVNC is copied in from dashboard/dist at install)
skills/handoff/SKILL.md
tests/          test_core.py (37) · test_sidecar.py (25) · e2e/run_e2e.py (dashboard, 25 checks) · e2e/run_webui_e2e.py (WebUI, 41 checks, incl. real touch gestures)
```

## Tests

```bash
V=<hermes venv python>
$V tests/run_all.py          # each file in its own process; real lease + RFB filter + real servers, fake screen
# end to end, scratch homes (touches nothing real). WebUI:
S=$(mktemp -d); W=$(mktemp -d)
HERMES_HOME=$S hermes plugins install file://$PWD --enable --yes-deps
HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W HERMES_WEBUI_PASSWORD=e2e-pass-123 HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=8798 $V ~/hermes-webui/server.py &
HANDOFF_PORT=17951 HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W hermes handoff webui
HANDOFF_PORT=17951 E2E_HOME=$S E2E_STATE=$W $V tests/e2e/run_webui_e2e.py
# dashboard: HERMES_HOME=$S HERMES_DASHBOARD_SESSION_TOKEN=e2e-session-token hermes dashboard --port 9199 --no-open --skip-build &
#            then  E2E_HOME=$S $V tests/e2e/run_e2e.py
```

Licenses: MIT; bundles noVNC 1.6.0 (MPL-2.0) and pako (MIT) unmodified under `dashboard/dist/novnc/`.
