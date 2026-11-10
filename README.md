# Takeover

**When your agent hits a step only you should do, it asks; you tap Take over, do it, and hand back.**

Signing in, typing a password, approving or authorizing an app ("Allow access", OAuth consent, "Approve sign-in"),
entering a 2FA code, clearing a CAPTCHA, confirming a payment: Hermes stops and calls `request_takeover("Sign in to
GitHub and approve the Hermes app")`. A banner with a one-tap **Take over** button appears on every page of
**hermes-webui**, the **Hermes dashboard** and **Hermes Desktop** (WebUI and the dashboard work from a phone). One tap opens the live view of the agent's Chrome and
gives you control. Do the step in that same browser and tap **Hand back**. The agent's waiting tool call returns by
itself and it carries on, signed in.

One native Hermes plugin. No tunnel, no reverse proxy, no extra port to open, no config to write.

## Install

```bash
hermes plugins install shoumikgoswami/Hermes-takeover --enable
```

That's it. Right after install (no new chat, no command) Takeover sets itself up in the background: the screen packages,
the agent's visible browser, and the hermes-webui Screen page and its service. **Reload WebUI once** (WebUI loads
extensions when a page loads) and the **Screen** button is in the side rail. The first time you open it, WebUI asks you
once to allow Takeover's service: tap **Allow Takeover**, then **Approve** on the highlighted row. Remove with
`hermes plugins remove takeover`; within a few seconds its service removes the Screen page from WebUI and exits.

| Surface | How it appears |
|---|---|
| **hermes-webui** | A **Screen** button in the side rail (phone: bottom nav); while the agent is asking, a banner on every page with **Take over** (one tap) and **Just watch**; a **Takeover** card in **Settings → Plugins** with live status and **Open Screen**. `<webui>/#takeover` opens it too. |
| **Hermes dashboard** (`hermes dashboard`) | A **Screen** tab and the same banner (one-tap **Take over** = `/takeover?take=1`). Restart the dashboard once if it was already running. |
| **Hermes Desktop** | An in-app alert with the reason, a native notification when Hermes is in the background, an **Agent needs you** chip in the status bar until it is answered, and a **Takeover** palette command. You take over in Desktop's own **Screen** pane (Agents, then the agent, then Screen, then **Take over**); it shares the lease this plugin waits on, so **Hand back** there lets the agent continue. See [Hermes Desktop](#hermes-desktop-and-other-hermes-products). |
| **Terminal, messaging apps** (`hermes`, Telegram, Discord, Slack, ...) | Nothing to look at there, so on a messaging platform the agent is told to message you first (what it needs, where to go), then waits. Open WebUI, the dashboard or Desktop to take over. |

### What "first run" does (automatically, once)

| Need | How it is handled |
|---|---|
| Screen packages (TigerVNC + Xfce) | Installed through **Hermes's own Bot Desktop installer** (the one the Desktop app's Install button uses). That installer escalates to root, so it works unattended only where root needs no password; otherwise Takeover shows the exact command and a **Run setup** button. |
| TigerVNC specifically | Many images ship a TightVNC `Xvnc`; it passes a "binary exists" check but cannot start Bot Desktop. Takeover detects it and says how to fix it. |
| Agent's Chrome visible | Sets `browser.headed: true` through the config API. |
| hermes-webui (if on this host) | Copies the Screen page into WebUI's extensions folder (loads on the next page render, no WebUI restart) and starts Takeover's small loopback service, detached. One service serves every profile. |
| WebUI's approval | **Your decision, one tap.** WebUI asks before an extension may use its own service (Settings → Extensions → Diagnostics); this one relays your clicks and keys into the agent's screen, so Takeover never grants it on its own by default. The Screen page shows **Allow Takeover**, which takes you to WebUI's exact Approve button, highlighted, and comes back by itself. WebUI only allows this when it has a password set. Opt in to `webui_auto_approve: true` if you want the page to grant it once by itself (a later revoke in WebUI still sticks). |
| Python packages | **None.** Everything is stdlib or already in Hermes; noVNC 1.6.0 is bundled (no CDN). |

When: the moment Hermes loads the plugin after install (a detached helper, so even a short `hermes ...` command starts it;
never from `plugins doctor` / `validate`), plus first Screen visit and first `request_takeover` as fallbacks, or by hand
with `hermes takeover setup` / `hermes takeover webui`. Package setup is attempted once automatically; afterwards only on request. Switches (`hermes config set plugins.entries.takeover.settings.<key> false`):
`auto_install` (packages + headed browser), `webui_integration` (never touch hermes-webui), `webui_auto_approve`, `fence_browser_tools`
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

**Agent side.** `request_takeover(reason, wait_seconds=900)` posts the request and waits until you hand back. Hermes
stops any single tool call after its tool deadline (420 s by default), so the call returns `still_waiting` shortly
before that and the agent calls again; the request card stays up the whole time, and two agents asking at once share
one card (both reasons shown):

| Result | Meaning |
|---|---|
| `completed` | You did the step and handed back. The agent takes a fresh screenshot and continues. |
| `still_waiting` | The call ran out of its own time; the agent calls again to keep waiting. |
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
- One WebUI service serves **every profile**: the page sends WebUI's active profile and the service uses that profile's
  Hermes home (lease, screen, requests), only where Takeover is installed. Switching profiles in WebUI switches the Screen
  page; a request from another profile still shows in the banner, naming the profile, and one tap answers it there.
  Port clash? `TAKEOVER_PORT=<n> hermes takeover webui` rewrites the extension to match. With several profiles on different
  Takeover versions, the newest one's page and service win (never downgraded).
- The WebUI service is started whenever Hermes loads the plugin (any agent session or `hermes` command); after a reboot the
  Screen page says the service is down until then. There is no systemd unit, by design.
- One pending request per profile: two agent sessions of the same profile asking at once share one request card.
- hermes-webui's own **Settings → Plugins** list only covers WebUI's own Hermes home (normally the default profile) and only
  refreshes when WebUI restarts; Takeover's page adds its card there itself, so it shows wherever it is installed. WebUI
  would also list the plugin's dashboard tab as "Screen" (a sandboxed page with no backend); that duplicate is hidden.

## Hermes Desktop and other Hermes products

| Product | What works | How it is wired |
|---|---|---|
| **hermes-webui** | Everything: Screen page, live view, banner, phone gestures, Plugins card. | Extension + loopback service (see above). Tested end to end (60 checks). |
| **Hermes dashboard** | Everything: Screen tab, live view, banner. | Dashboard plugin (`dashboard/`). Tested end to end against a real dashboard, real Xvnc and Chromium (25 checks). |
| **Hermes Desktop** | The agent's request reaches you (alert, native notification, status-bar chip, palette command); taking over uses Desktop's own Screen pane. | `desktop/plugin.js` (loaded by Desktop with the plugin, no separate install) listens to `plugin.takeover.request` / `ended`, which the tool emits through Hermes' public `plugin_events` door. The tool, the lease and the fence run unchanged. |
| **Terminal** (`hermes`, `hermes chat`) | The agent can ask and wait; take over from any of the screens above. | Nothing to show in a terminal. |
| **Messaging platforms** | The agent is told to message you first, then waits. | `request_takeover` returns `requested_tell_user` once per conversation; the second call waits. |

Desktop notes:
- The Desktop half is a separate switch in Desktop (**Capabilities → Plugins**, "Takeover"); Hermes ships Desktop halves of
  installed packages opt-in, so enable it there once. The agent half works without it (the agent still asks and waits; you
  just are not alerted in the app).
- It does not draw the screen itself: Desktop's Screen pane is part of its Bots feature and has no public SDK entry, so
  the alert points you there instead of duplicating a viewer. Against a **remote** backend Desktop cannot copy the half
  from the remote machine's plugins folder; install it from Git with the Desktop target checked.
- The alert is also polled every 15 s (`/api/plugins/takeover/pulse`), so a window opened after the request was posted
  still shows it.
- Not tested against a running Desktop app here (no Electron on this server): the Desktop half is tested against the
  documented SDK contract with a mocked SDK (`tests/desktop/`), not inside the app.

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
                surfaces (events to Desktop, messaging-platform detour)
dashboard/      manifest.json · plugin_api.py (routes + WebSocket) · dist/ (index.js, style.css, novnc/ 1.6.0 vendored)
desktop/        plugin.js (Hermes Desktop half: alert, notification, status-bar chip; no JSX, SDK-only imports)
webui/          manifest.json · assets/takeover.js + takeover.css + touch.js (phone gestures)   (noVNC is copied in from dashboard/dist at install)
skills/takeover/SKILL.md
tests/          test_core.py (65) · test_sidecar.py (43) · desktop/ (10, node, mocked SDK) · test_hermes_integration.py (6: through Hermes's own plugin loader,
                hook dispatch, prompt rendering, `plugins doctor --ci`, `plugins validate`, first run right after install)
                e2e/run_e2e.py (dashboard, 25) via e2e/fresh_dashboard_e2e.sh · e2e/run_webui_e2e.py (WebUI, 60) via e2e/fresh_webui_e2e.sh (scratch homes)
```

## Tests

Following the [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins): `hermes plugins doctor . --ci`
(discovery, manifest, `register(ctx)`, declared vs registered tools/hooks, in a temporary home) and `hermes plugins
validate .` (the catalog checks: security scan, no core override, declared hooks). Both run inside the test suite. The only
validate warning is the private Bot Desktop / interrupt imports, which have no public equivalent yet.

```bash
V=<hermes venv python>
$V tests/run_all.py          # unit + Hermes-integration + Desktop-half tests, each in its own process (commit first: install clones git)
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
