## Takeover is installed

When a step needs you (sign in, approve or authorize, a 2FA code, a CAPTCHA, a payment), the agent asks and you tap
**Take over**. You do it in the agent's browser and tap **Hand back**.

**Where to find it**
- **hermes-webui**: reload the page once. The **Screen** button is in the side rail (phone: bottom nav), Takeover is in
  **Settings → Plugins**, and a banner with **Take over** appears whenever the agent asks. Nothing to approve: enabling
  the plugin is the consent (WebUI needs a password set for that; the Screen page tells you if it doesn't have one).
- **Hermes dashboard**: a **Screen** tab (restart `hermes dashboard` if it was already running).
- **Terminal**: `hermes takeover status`

**Already happening in the background:** installing the screen packages (TigerVNC + Xfce, through Hermes's own Bot Desktop
installer), turning on `browser.headed`, and adding the Screen page and its service to hermes-webui. If this server needs
a password for root, `hermes takeover status` and the Screen page show the one command to run.

New chats know when to ask for you. Feedback: hello@shoumikgoswami.com
