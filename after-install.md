## Takeover is installed

When a step needs you (sign in, approve or authorize, a 2FA code, a CAPTCHA, a payment), the agent asks and you tap
**Take over**. You do it in the agent's browser and tap **Hand back**.

**Where to find it**
- **hermes-webui**: reload the page once. The **Screen** button is in the side rail (phone: bottom nav), Takeover is in
  **Settings → Plugins**, and a banner with **Take over** appears whenever the agent asks. The first time, WebUI
  asks you once to allow Takeover's service: open **Screen**, tap **Allow Takeover**, then **Approve** on the highlighted
  row (WebUI needs a password set; the Screen page tells you if it doesn't have one).
- **Hermes dashboard**: a **Screen** tab (restart `hermes dashboard` if it was already running).
- **Hermes Desktop**: turn on **Takeover** once in **Capabilities → Plugins** for the in-app alert, notification and status-bar chip.
  You take over in Desktop's own **Screen** pane (Agents → the agent → Screen → Take over).
- **Telegram, Discord, Slack...**: the agent messages you where to go, then waits; take over from WebUI, the dashboard or Desktop.
- **Terminal**: `hermes takeover status`

**Already happening in the background:** adding the Screen page and its service to hermes-webui (nothing privileged).
**On first use** (opening Screen, the agent's first request, or `hermes takeover setup`): installing the screen packages
(TigerVNC + Xfce, through Hermes's own Bot Desktop installer) and turning on `browser.headed`, once. If this server needs
a password for root, the Screen page and `hermes takeover status` show the one command to run.

New chats know when to ask for you. Feedback: hello@shoumikgoswami.com
