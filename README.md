# Handoff

**Take over your agent's browser, do the one step only you can do, hand it back.**

When the agent hits a login, a 2FA code, a CAPTCHA or a payment page, it calls `request_handoff`. You get a
**Screen** tab in the Hermes dashboard (works from a phone) showing the agent's Chrome live. Tap **Take over**,
complete the step in that same browser, tap **Hand back**. The agent's waiting tool call returns by itself and it
continues, logged in.

A native Hermes plugin: no sidecar, no extra port, no tunnel, no config. The dashboard serves everything.

## Install

```bash
hermes plugins install <owner>/hermes-handoff --enable      # or any git URL
hermes dashboard                                            # Screen tab appears; restart it if it was already running
```

That is all. On first use Handoff sets itself up (below). Remove with `hermes plugins remove handoff`.

### What "first run" does (automatically, once)

| Need | How it is handled |
|---|---|
| Screen packages (TigerVNC + Xfce) | Installed through **Hermes's own Bot Desktop installer** (the one the Desktop app's Install button uses). That installer escalates to root, so it works unattended only where root needs no password; otherwise Handoff shows you the exact command to run and a **Run setup** button. |
| TigerVNC specifically | Many images ship a TightVNC `Xvnc`; it passes a "binary exists" check but cannot start Bot Desktop. Handoff detects it and tells you how to point `Xvnc` at TigerVNC. |
| Agent's Chrome visible | Sets `browser.headed: true` through the config API. |
| Python packages | **None.** FastAPI/websockets come with the dashboard; noVNC 1.6.0 is bundled (no CDN). |

Triggered by the first Screen-tab visit, the first `request_handoff` call, or `hermes handoff setup`. It runs once
automatically; after that only on request. Opt out: `hermes config set plugins.entries.handoff.settings.auto_install false`.
Linux hosts only (Bot Desktop is Linux-only); the tool hides itself elsewhere.

## Using it

**Agent side.** The tool `request_handoff(reason, wait_seconds=900)` posts the request and waits until you hand back:

| Result | Meaning |
|---|---|
| `completed` | You did the step and handed back. The agent takes a fresh screenshot and continues. |
| `timeout` / `still_with_human` / `dismissed` / `cancelled` | The agent tells you what it is waiting for and stops. |

While you hold control every screen/browser tool is refused (`HumanHasControl`), even screenshots, because you may be
typing a credential. A bundled skill (`handoff:handoff`) teaches the agent when to ask and never to request secrets in chat.

**Your side.** Dashboard → **Screen**. While the agent is asking, an amber banner shows on *every* dashboard page.

- Status line says who is driving. The video is view-only until you **Take over** (enforced server-side, not just in the UI).
- Phone: tap = click · two-finger tap = right click · hold then drag = drag · two-finger drag = scroll · **Zoom** toggles fit / 100% with panning.
- **⌨ Keyboard** opens the phone keyboard. **Type text** sends a pasted string as keystrokes (best for passwords with symbols).
  Esc / Tab / Enter / Ctrl+L / Ctrl+A / Ctrl+V are always there. **Paste from phone** needs HTTPS or localhost.
- Closed the tab mid-takeover? The agent stays locked out until you reopen Screen and press **Force hand-back**
  (or run `hermes handoff release`, or `/handoff release`). A second device's Take over evicts the first.

**CLI.** `hermes handoff status | setup | screen | release` and `/handoff` in any chat.

## Security model

- HTTP routes sit behind the dashboard's own authentication. The WebSocket stream is **not** covered by HTTP middleware,
  so it is admitted only with a **single-use 30-second ticket** minted through an authenticated route, plus the dashboard's
  own Host / Origin / peer check. Tickets ride in the URL (noVNC cannot send headers) and are spent on first use.
- The server-side RFB filter drops key / pointer / clipboard / resize messages unless the lease says *this* viewer holds
  it. noVNC's view-only flag is only UX.
- The lease is a tool-level fence, not an OS boundary: anything you type is visible to the agent's browser. Do not enter a
  secret you would not trust the agent with.
- Logs never contain tickets, full viewer ids, keystrokes or frames.
- The plugin runs with the dashboard's permissions, like any Hermes plugin. Its only privileged action is the package
  install, delegated to Hermes's Bot Desktop installer; it has no code path of its own that elevates privileges.

## Requirements and limits

- Hermes with Bot Desktop (`tools.bot_desktop`); the plugin verifies the symbols it uses on first use and says so if a Hermes
  update changed them (`hermes plugins update handoff`). `hermes plugins validate` lists these private imports; Bot Desktop has no public plugin API.
- The dashboard must keep `plugins.isolation: process` (the default): the live stream needs the in-process route.
- One screen per Hermes profile (the lease lives in the profile's home). The dashboard's profile scoping is respected.
- The **hermes-webui** project (a separate app) does not load Hermes dashboard plugins; use `hermes dashboard`.
- The agent's browser must be headed (`browser.headed`) and started *after* setup; an already-running headless Chrome shows an empty desktop.

## Layout

```
plugin.yaml  __init__.py        tool, CLI, slash command, bundled skill
handoff_core/                   compat (hermes-agent seam), setup (first run), control, bridge (RFB<->WS), tickets, tool, cli
dashboard/                      manifest.json, plugin_api.py (routes + stream), dist/index.js + style.css, dist/novnc/ (vendored 1.6.0)
skills/handoff/SKILL.md
tests/test_core.py              36 tests: real lease + RFB filter + uvicorn + websockets, fake screen
tests/e2e/                      real dashboard + real Xvnc/Xfce + real Chromium (25 checks)
```

## Tests

```bash
V=<hermes venv python>
$V -m unittest discover -s tests -v                      # core, routes, bridge gating, tool semantics, first-run policy
# end to end (scratch HERMES_HOME so nothing real is touched):
HERMES_HOME=$S hermes plugins install file://$PWD --enable --yes-deps
HERMES_HOME=$S HERMES_DASHBOARD_SESSION_TOKEN=e2e-session-token hermes dashboard --port 9199 --no-open --skip-build &
E2E_HOME=$S $V tests/e2e/run_e2e.py
```

Licenses: MIT; bundles noVNC 1.6.0 (MPL-2.0) and pako (MIT) unmodified under `dashboard/dist/novnc/`.
