## Takeover is installed

When a step needs you (sign in, approve or authorize, a 2FA code, a CAPTCHA, a payment), the agent asks and you tap
**Take over**. You do it in the agent's browser and tap **Hand back**.

**Where to find it**
- **hermes-webui**: reload the page. You'll see a **Screen** icon in the side rail (phone: bottom nav) and a banner with a
  **Take over** button whenever the agent asks. The first time, the Screen page shows **Allow Takeover**. Tap it, then
  tap **Approve proxy consent** on the highlighted row. That's WebUI's own one-time security consent.
- **Hermes dashboard**: a **Screen** tab (restart `hermes dashboard` if it was already running).
- **Terminal**: `hermes takeover status`

**Already happening in the background:** setting up the screen (TigerVNC + Xfce through Hermes's Bot Desktop installer,
and `browser.headed`) and adding the Screen page to hermes-webui. If installing the packages needs a password,
`hermes takeover status` and the Screen page show the one command to run.

New chats know when to ask for you. Feedback: hello@shoumikgoswami.com
