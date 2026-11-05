---
name: handoff
description: "Use when a browser step needs the human (login, 2FA, CAPTCHA, payment, consent). Call request_handoff, wait, then resume."
---

# Handoff: asking the human to take over the screen

The user can open the Hermes dashboard's **Screen** tab (works from a phone), see your Chrome live, tap
**Take over**, do the step, and **Hand back**. What they do happens in *your* browser profile, so a login they
complete is *your* login.

## When to call `request_handoff`
Whenever the next step is one you must not or cannot do yourself:
- a login, password, 2FA/SMS/OTP code, passkey, device approval
- a CAPTCHA or "verify you are human" check
- a payment, card entry, or irreversible confirmation the user should own
- an unexpected consent / terms page you were not told to accept

## How
1. Call `request_handoff(reason="<one sentence: exactly what to do>")`. Example reason: "Enter the SMS code for your bank login".
2. The call **waits** (up to 15 min by default) until the human hands control back. You do not need them to say "done".
3. Result `completed`: take a **fresh screenshot/snapshot first**, the page has changed, then continue.
4. Result `timeout` / `still_with_human` / `dismissed`: tell the user plainly what you are waiting for and **stop the turn**.
   Do not retry in a loop.

## While the human has control
Every screen/browser tool is refused with `HumanHasControl` (even screenshots, they may be typing a credential).
Do not work around it (no terminal tricks against the display) and do not poll.

## Never
- Ask the user to paste a password, code or card number into chat.
- Read back, log or store what they entered.
- Try to take or release control yourself.
