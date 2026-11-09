---
name: takeover
description: "Use when a browser step needs the human (sign-in, approve/authorize, 2FA, CAPTCHA, payment). Call request_takeover, wait, resume."
---

# Takeover: asking the human to take over the screen

The user can open the **Screen** page in hermes-webui or the Hermes dashboard (works from a phone), see your Chrome live, tap
**Take over**, do the step, and **Hand back**. What they do happens in *your* browser profile, so a login they
complete is *your* login.

## When to call `request_takeover`
Whenever the next step on screen is one only the user should do:
- typing credentials: a password, sign-in form, passkey, account recovery
- approving or authorizing: "Authorize <app>", "Allow access", OAuth consent, "Approve sign-in", device approval
- a 2FA / SMS / authenticator / email code
- a CAPTCHA, "verify you are human" check or bot wall
- a payment, card entry, or irreversible confirmation the user should own
- an unexpected consent / terms page you were not told to accept
A browser result may carry a `takeover_hint` / "[Takeover] ..." note when the page looks like one of these; treat it as a
strong signal, but you decide (ignore it if the page does not actually need the user).

## How
1. Call `request_takeover(reason="<one sentence: exactly what to do>")`. Example reasons: "Sign in to GitHub and approve the Hermes app", "Enter the SMS code for your bank login". The user gets a one-tap Take over button.
2. The call **waits** until the human hands control back. You do not need them to say "done". Hermes caps one tool call at a few
   minutes: on `still_waiting`, call `request_takeover` again with the same reason (it joins the same request, no second card).
3. Result `completed`: take a **fresh screenshot/snapshot first**, the page has changed, then continue.
4. Result `timeout` / `still_with_human` / `dismissed` (or `still_waiting` after you have re-called a few times): tell the user plainly what you are waiting for and **stop the turn**.
   Do not retry in a loop.

## While the human has control
Every screen/browser tool is refused (even screenshots and raw CDP; they may be typing a credential).
Do not work around it: no terminal, code-execution or CDP-client tricks against the display or the browser, and do not poll.

## Never
- Ask the user to paste a password, code or card number into chat.
- Read back, log or store what they entered.
- Try to take or release control yourself.
