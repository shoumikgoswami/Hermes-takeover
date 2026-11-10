## Takeover is installed

When a step needs you (sign in, approve or authorize, a 2FA code, a CAPTCHA, a payment), the agent asks and you tap
**Take over**, do it in the agent's browser, and tap **Hand back**. The agent then carries on by itself.

**Get started in hermes-webui (once)**
1. Reload WebUI. A **Screen** button appears in the side rail (phone: bottom nav).
2. Open **Screen** and tap **Allow Takeover**, then **Approve proxy consent** on the highlighted row. WebUI asks this once
   because the Screen passes your clicks and keys to the agent's browser. WebUI needs a password set (Settings → Password).
3. The screen then sets itself up, once: the screen packages and the agent's visible browser. It takes a minute or two
   and the page shows progress. If the server needs a root password, the page shows the one command to run, with a
   **Copy command** button.

**Other places**
- **Hermes dashboard**: a **Screen** tab (restart `hermes dashboard` if it was already running). Same steps, without the
  WebUI approval.
- **Hermes Desktop**: turn on **Takeover** once in **Capabilities → Plugins** for the alert, notification and status-bar
  chip. You take over in the bot's own **Screen** (the Screen box at the top of its pane, or right-click the bot →
  **Open Screen**, then **Take over**).
- **Telegram, Discord, Slack...**: the agent messages you what it needs and where to go, then waits.
- **Terminal**: `hermes takeover status` shows the state and the one next step.

Nothing privileged runs until you first use it (opening Screen, the agent's first request, or `hermes takeover setup`).
New chats know when to ask for you. Feedback: hello@shoumikgoswami.com
