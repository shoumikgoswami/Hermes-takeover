# Takeover

**When your agent hits a step only you should do, it asks; you tap Take over, do it, and hand back.**

Signing in, typing a password, approving or authorizing an app ("Allow access", OAuth consent, "Approve sign-in"),
entering a 2FA code, clearing a CAPTCHA, confirming a payment: Hermes stops and calls `request_takeover("Sign in to
GitHub and approve the Hermes app")`. A banner with a one-tap **Take over** button appears on every page of
**hermes-webui** and the **Hermes dashboard** (works from a phone). One tap opens the live view of the agent's Chrome and
gives you control. Do the step in that same browser and tap **Hand back**. The agent's waiting tool call returns by
itself and it carries on, signed in.

One native Hermes plugin. No tunnel, no reverse proxy, no extra port to open, no config to write.

## Install

```bash
hermes plugins install shoumikgoswami/Hermes-takeover --enable
```

Then just use Hermes. The first agent session wires everything up (below). Remove with `hermes plugins remove takeover`;
within a few seconds its WebUI service notices, removes the Screen page from WebUI and exits.

| Surface | How it appears | Your one manual step |
|---|---|---|
| **hermes-webui** (the `:8787` app) | A **Screen** icon in the rail / phone nav; while the agent is asking, a banner on every page with **Take over** (one tap) and **Just watch**. A link to `<webui>/#takeover` does the same. | Settings → Extensions → Diagnostics → Loopback sidecars → **Takeover** → **Approve proxy**. That is WebUI's own security consent, and WebUI authentication must be on (Settings → set a password). The Screen page tells you this until you do it. |
| **Hermes dashboard** (`hermes dashboard`) | A **Screen** tab; the same banner on every page (one-tap **Take over** = `/takeover?take=1`) | none (restart the dashboard once if it was already running) |

### What "first run" does (automatically, once)

| Need | How it is handled |
|---|---|
| Screen packages (TigerVNC + Xfce) | Installed through **Hermes's own Bot Desktop installer** (the one the Desktop app's Install button uses). That installer escalates to root, so it works unattended only where root needs no password; otherwise Takeover shows the exact command and a **Run setup** button. |
| TigerVNC specifically | Many images ship a TightVNC `Xvnc`; it passes a "binary exists" check but cannot start Bot Desktop. Takeover detects it and says how to fix it. |
| Agent's Chrome visible | Sets `browser.headed: true` through the config API. |
| hermes-webui (if on this host) | Copies the Screen page into WebUI's extensions folder (loads on the next page render, no WebUI restart) and starts Takeover's small loopback service, detached. The next agent session restarts it if it is not running. |
| Python packages | **None.** Everything is stdlib or already in Hermes; noVNC 1.6.0 is bundled (no CDN). |

Triggers: first agent session, first Screen visit, first `request_takeover`, or `hermes takeover setup` / `hermes takeover webui`.
It runs once automatically; afterwards only on request. Switches (`hermes config set plugins.entries.takeover.settings.<key> false`):
`auto_install` (packages + headed browser), `webui_integration` (never touch hermes-webui), `fence_browser_tools`
(pause browser tools while you hold control) and `takeover_hints` (the nudge below). Linux only (Bot Desktop is
Linux-only); the tool hides itself elsewhere.

## Using it

**When the agent asks.** Three things make Hermes hand over instead of guessing, typing your password or getting stuck:

1. A **system prompt section** (`takeover.hand-over-to-user`, ~1.2 KB, frozen per session so it never breaks prompt
   caching) lists the steps that are yours: credentials and sign-in, approve / authorize / consent screens, 2FA and device
   approval codes, CAPTCHAs and bot walls, payments. It tells the agent never to ask for secrets in chat.
2. A **hint on browser results**: when a page the agent reads shows a password field, an "Authorize"/"Allow" consent, a
   one-time-code prompt, a card form or a CAPTCHA, the result carries a short "call request_takeover" note (once per
   conversation and kind; never while you already hold control; opt out with `takeover_hints: false`).
3. The tool description itself, and the bundled skill `takeover:takeover`.

**Agent side.** `request_takeover(reason, wait_seconds=900)` posts the request and waits until you hand back:

| Result | Meaning |
|---|---|
| `completed` | You did the step and handed back. The agent takes a fresh screenshot and continues. |
| `timeout` / `still_with_human` / `dismissed` / `cancelled` | The agent tells you what it is waiting for and stops. |

While you hold control every screen/browser tool is refused, even screenshots, because you may be typing a credential.
Hermes fences its own local browser sessions and `computer_use`; Takeover adds a `pre_tool_call` veto on every `browser_*`
tool and `computer_use` for the profile, which also covers what Hermes leaves open: a browser reached through
`browser.cdp_url` and the raw `browser_cdp` tool (opt out: `fence_browser_tools: false`). It cannot fence `terminal` or
code execution, so the agent could still reach the browser through a shell (see Security model).

**Your side.** Tap **Take over** in the banner (or open **Screen** and tap it there). The status line says who is
driving; the video is view-only until you take over (enforced server-side, not just in the UI).

- Phone (hermes-webui): **drag = scroll** (the page follows your finger, with momentum) · tap = click · hold, then drag = select text / drag
  and drop · pinch = zoom and pan · two-finger tap = right click. **More** shows zoom −/+/Fit/Reset, Ctrl+L/A/V and Type text. While you are only
  watching, a drag pans the view. A mouse works too (wheel scrolls, Ctrl+wheel zooms). The stage fills about three quarters of a phone screen.
- The dashboard Screen tab still uses noVNC's own touch handling (two-finger drag = scroll).
- **⌨ Keyboard** opens the phone keyboard. **Type text** sends a pasted string as keystrokes (best for passwords with symbols).
  Esc / Tab / Enter / Ctrl+L / Ctrl+A / Ctrl+V are always there. **Paste from phone** needs HTTPS or localhost.
- Closed the tab mid-takeover? The agent stays locked out until you reopen Screen and press **Force hand-back**
  (or `hermes takeover release`, or `/takeover release`). A second device's Take over evicts the first.

**CLI.** `hermes takeover status | setup | screen | release | webui [--remove]` and `/takeover` in any chat.

## How it works

```
agent process                         hermes-webui (whatever host/port you already use, incl. over Tailscale)
 request_takeover ─┐                    browser ── /api/extensions/takeover/sidecar/* ──► WebUI proxy (your login + CSRF + consent)
 hermes takeover   ├─ takeover_core ─┐                                                     │ adds X-Hermes-Sidecar-Token
 dashboard routes ┘  (lease, setup,│                                                     ▼
                      requests)    ├─ Bot Desktop lease file  ◄── loopback sidecar 127.0.0.1:17901 (stdlib, detached)
hermes dashboard ── /api/plugins/takeover/* ──────────────────────┘     RfbClientFilter + lease gate ──► Xvnc ── agent's Chrome
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
- The lease is a tool-level fence, not an OS boundary: the agent's browser tools are paused, but the browser itself is the
  agent's, and a determined agent could read the page through `terminal` / code execution (e.g. its own CDP client). Don't
  enter a secret you would not trust the agent with. Input from a viewer that just lost control can still land for up to
  0.25 s (the sidecar re-reads the lease at most four times a second).
- Logs never contain tickets, stream session ids, full viewer ids, keystrokes or frames (the sidecar logs no URLs).
- The plugin runs with your user's permissions, like any Hermes plugin. Its only privileged action is the package install,
  delegated to Hermes's Bot Desktop installer; it has no code path of its own that escalates privileges. Its only writes
  outside its data dir (`<profile>/plugin-data/takeover/`): `browser.headed` in config, and (WebUI) the extension folder + install manifest in WebUI's state dir.
- A WebUI service serves **one Hermes profile** (the lease lives in the profile's home). The first profile to start it wins;
  `hermes takeover webui` from another profile takes it over; `hermes takeover status` says when it is serving a different one.
  Port clash? `TAKEOVER_PORT=<n> hermes takeover webui` rewrites the extension to match. Switching profiles inside WebUI does
  not switch the Screen page; it keeps showing the profile the service serves.
- The WebUI service is started by an agent session (or `hermes takeover webui`); after a reboot the Screen page says the
  service is down until the next agent session starts it. There is no systemd unit, by design.
- One pending request per profile: two agent sessions of the same profile asking at once share one request card.

## Requirements and limits

- Hermes with Bot Desktop (`tools.bot_desktop`). The plugin verifies the symbols it uses and says so if a Hermes update changed
  them (`hermes plugins update takeover`). `hermes plugins validate` lists these private imports; Bot Desktop has no public plugin API.
- Dashboard: keep `plugins.isolation: process` (default); the live stream needs the in-process route.
- The agent's browser must be headed and started *after* setup; an already-running headless Chrome shows an empty desktop.
- hermes-webui: authentication must be on (WebUI's rule for token-protected extension backends). Extensions run with full WebUI
  session authority; this one is small, has no third-party scripts, and noVNC is vendored.
- Remote scrolling is sent as mouse-wheel notches (one notch = about 96 px in Chrome); other apps scroll by other amounts per notch, so the
  finger tracking is exact only in Chrome-like apps (constant `NOTCH_PX` in `touch.js`).
- The long-poll channel adds about one HTTP round trip of latency per input; fine for forms, not for games.

## Layout

```
plugin.yaml  __init__.py  sidecar_run.py   tool, CLI, /takeover, hooks, prompt section, bundled skill; detached sidecar entry
takeover_core/  compat (hermes-agent seam) · setup (first run) · control (lease ops) · state · tickets · bridge (dashboard WS)
                sidecar (hermes-webui backend + long-poll stream) · webui (install extension, keep service up) · tool · cli
                fence (pre_tool_call veto while you drive) · nudge (prompt section + "ask the user" hint on browser results)
dashboard/      manifest.json · plugin_api.py (routes + WebSocket) · dist/ (index.js, style.css, novnc/ 1.6.0 vendored)
webui/          manifest.json · assets/takeover.js + takeover.css + touch.js (phone gestures)   (noVNC is copied in from dashboard/dist at install)
skills/takeover/SKILL.md
tests/          test_core.py (51) · test_sidecar.py (27) · test_hermes_integration.py (5: through Hermes's own plugin loader,
                hook dispatch, prompt rendering, `plugins doctor --ci` and `plugins validate`)
                e2e/run_e2e.py (dashboard, 25) · e2e/run_webui_e2e.py (WebUI, 44, incl. touch gestures and the one-tap banner)
```

## Tests

Following the [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins): `hermes plugins doctor . --ci`
(discovery, manifest, `register(ctx)`, declared vs registered tools/hooks, in a temporary home) and `hermes plugins
validate .` (the catalog checks: security scan, no core override, declared hooks). Both run inside the test suite. The only
validate warning is the private Bot Desktop / interrupt imports, which have no public equivalent yet.

```bash
V=<hermes venv python>
$V tests/run_all.py          # unit + Hermes-integration tests, each file in its own process (commit first: install clones git)
# end to end, scratch homes (touches nothing real). WebUI:
S=$(mktemp -d); W=$(mktemp -d)
HERMES_HOME=$S hermes plugins install file://$PWD --enable --yes-deps
HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W HERMES_WEBUI_PASSWORD=e2e-pass-123 HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=8798 $V ~/hermes-webui/server.py &
TAKEOVER_PORT=17951 HERMES_HOME=$S HERMES_WEBUI_STATE_DIR=$W hermes takeover webui
TAKEOVER_PORT=17951 E2E_HOME=$S E2E_STATE=$W $V tests/e2e/run_webui_e2e.py
# dashboard: HERMES_HOME=$S HERMES_DASHBOARD_SESSION_TOKEN=e2e-session-token hermes dashboard --port 9199 --no-open --skip-build &
#            then  E2E_HOME=$S $V tests/e2e/run_e2e.py
```

## Feedback

Ideas, bugs or questions: [hello@shoumikgoswami.com](mailto:hello@shoumikgoswami.com?subject=Takeover%20plugin%20feedback), or open an issue.
The same address is in the plugin's side panel in hermes-webui.

Licenses: MIT; bundles noVNC 1.6.0 (MPL-2.0) and pako (MIT) unmodified under `dashboard/dist/novnc/`.
