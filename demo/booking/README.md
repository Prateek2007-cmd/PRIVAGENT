# Demo Booking Fixture — flow script (Stream F)

Controlled, DOM-based booking demo for the NAVIGATOR demo, Fri 2026-11-27.
No backend, no payment, no real data. The fixture is four static pages
served from this directory; the agent drives them through the normal
observe → plan → act → verify loop using accessibility-tree refs.

## Serve it

```bash
# Linux / macOS, from the repo root:
python3 -m http.server 8123 --directory demo/booking
# Windows (PowerShell), from the repo root:
python -m http.server 8123 --directory demo\booking
# Fixture base URL:
http://127.0.0.1:8123/index.html
```

Smoke-check (no browser needed): `python3 demo/booking/smoke-check.py`

## Pages

| Step | Page | What the agent does |
|---|---|---|
| 1 | `index.html` | Search: From, To, date (native date input), passengers, class → **Search trips** |
| 2 | `results.html` | Compare 3 options (times + ₹ fare) → **Select …** link |
| 3 | `passengers.html` | Passenger form (name, age, email, phone, berth) → **Review booking** |
| 4 | `summary.html` | Booking summary + total → **STOP. Pay button is disabled.** Status `prepared`. |

Synthetic route data: Hyderabad (HYD) → Bengaluru (BLR) has three named
trains (Demo Express 12639 ₹450 Sleeper, Demo Shatabdi 12027 ₹720 3AC,
Demo Vande Bharat 20607 ₹990 2AC). Every other route shows three generic
Demo Railways options (₹520 / ₹780 / ₹1150). Default date 2026-12-05,
default passenger "Demo Traveller", 21, demo@example.test, 90000 00000.
All clearly fictional — never use real personal data here.

## Why this fixture (and not the old canvas demo)

The planner's hardcoded deterministic path (`planner.py:710-772`,
"visual-controls" → Seat 14B at coords (270,225), date 27 Nov 2026 at
(470,135)) belongs to Stream B — Stream F does not touch it — and the
page it points at (`/demo/visual-controls.html`) is **not in this repo**,
so it cannot be the demo. This fixture is DOM-based on purpose: every
control is a labelled input/button/select, so the generic AX-tree path
works and the demo proves generalisation instead of a hardcoded
coordinate click. Do not present the visual-controls path as general
canvas ability (RC-1.4).

## Demo flow — NL prompts, in order

Run as ONE task per step (or one task for the whole flow if the loop
holds state — prefer one task per step on stage; a failure then costs
one step, not the whole booking). Pre-fill everything: the agent cannot
ask for missing information yet (RC-1.1 / R5-07 — the ask-user
pause/resume mechanism is Stream D's build, not yet live).

### Step 0 — Missing-info ask (uses the §2 ask-user contract)

Prompt (deliberately incomplete — shows the ask path **if** Stream D
shipped it; otherwise the task fails honestly, see fallback):

> "Book me a train to Bengaluru."

- **Expected if ask-user shipped (Stream D):** task pauses in
  `awaiting_input` and the workspace shows an `ASK_USER` question
  (`kind: "missing_info"`, e.g. "What date should I book for?" /
  "Where are you travelling from?"), with `question_id`, `timeout_s:
  300`. Answering resumes from the checkpoint without restarting.
  **Never** a sensitive question in chat — dates, stations and budgets
  are fine; passwords, OTPs and card details go through takeover only.
- **Expected if ask-user did NOT ship:** the task fails honestly with a
  clarification message ("I need a date and a departure station").
  That is the pre-fix behaviour — say so out loud and move to Step 1
  with the complete prompt. Do not improvise a date on stage.

### Step 1 — Travel requirements (complete prompt)

> "Find train options from Hyderabad to Bengaluru for 2026-12-05, 1 passenger, Sleeper preferred, under ₹1000. Compare them and prepare the best booking for my approval. Do not pay anything."

- **Expected:** the agent navigates to the fixture search page, fills
  From = Hyderabad (HYD), To = Bengaluru (BLR), date = 2026-12-05,
  passengers = 1, class = Sleeper, and submits. (If it goes to a real
  travel site first — that is the "website discovery" step below; the
  fixture is the controlled fallback when live sites block it.)
- **Fallback:** "The live site needs a login / shows a CAPTCHA, so I am
  using the controlled fixture instead — same flow, synthetic data."

### Step 2 — Website discovery (research, labelled)

If the agent researches real travel sources first (IRCTC / ixigo /
MakeMyTrip / Google Travel), that research is **research, not booking**:
say "these are public sources I checked for times and prices; the
booking itself runs on the controlled fixture." Do not claim a live
price as the fixture price. If a live site blocks the agent (login wall,
CAPTCHA), that is a handoff, not a failure to hide — the deterministic
planner hands login/CAPTCHA to the operator.

### Step 3 — Date selection

- **Expected:** the date input shows 2026-12-05 (native `<input
  type="date">`, driven via `set_date` / typed as 2026-12-05). The
  planner must use the date exactly as given — never change the year
  (site_hints google.com rule; same discipline here).
- **Fallback:** if the date control misbehaves, type the date and verify
  the read-back value before continuing.

### Step 4 — Availability research + option comparison

- **Expected:** results page shows three options with times and fares;
  the agent compares them and picks under-budget: Demo Express 12639,
  Sleeper, ₹450 (cheapest, under ₹1000) — or states why it picked the
  ₹720 3AC instead (e.g. faster arrival). Either is a good answer if
  the reason is stated and the fare is quoted from the page.
- **Fallback:** if only one option renders, say so — do not invent the
  other two.

### Step 5 — Controlled fixture navigation

- **Expected:** click **Select Demo Express 12639, ₹450** → passenger
  form. The click quotes an AX ref (e.g. `e7`), never a CSS selector.
- **Fallback:** if the click misses, the observation after the click
  shows whether the URL changed; re-ground from the fresh observation.

### Step 6 — Passenger form with synthetic data

> (continuing the same task) "Passenger: Demo Traveller, 21, demo@example.test, 90000 00000, Lower berth."

- **Expected:** every field filled and read back; **Review booking**
  submits to the summary.
- **Do not:** type any real name, email or phone. The fixture defaults
  are already the synthetic passenger — the agent may accept the
  defaults and say so.

### Step 7 — Booking summary → STOP at the approval/payment boundary

- **Expected:** summary page shows route, date, option, times, class,
  passenger, contact, fare ₹450, total ₹450, and status **PREPARED —
  awaiting approval**. The agent reports the summary and stops. The
  "Pay & confirm" button is disabled; the agent must never click past
  this page.
- **Say on stage:** "The booking is prepared, not completed. Nothing is
  reserved, nothing is paid. Approval — and any payment — is a human
  step after this screen."
- **Status vocabulary (§6):** `prepared` (ready, awaiting approval).
  Never "completed", never "booked", never "sent".

## Acceptance test 13 (controlled booking reaches approval boundary)

- **Pass:** summary page reached with correct route/date/option/fare/
  total, status reported as `prepared`, no payment attempted, no real
  site account used.
- **Fail (honest):** `EXTENSION_DISCONNECTED`, a named `STALE_TARGET`
  / `NAVIGATION_FAILED` with the step + ref, or an ask-user timeout
  (`ASK_USER_TIMEOUT`) — each is a named failure, not a fake success.
- **UNVERIFIED in this sandbox:** no browser task has ever run here
  (no Chrome, no extension). Verify on Windows with
  `navigator-audit/reports/LOCAL_BROWSER_VERIFICATION.md`.

## What must NOT be demoed with this fixture

- A real booking, payment, or reservation on any live site.
- The old visual-controls canvas path as "general" ability (RC-1.4).
- A date, station or budget the user did not give (no assumed defaults
  presented as the user's choice — §15 assume-defaults is the known gap).
- Any real personal data in the passenger form.
