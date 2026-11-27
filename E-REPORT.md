# Stream E — Browser + Privacy — Report

Worktree: `~/workspace/navigator-audit/worktrees3/stream-E` (HEAD `0cae209`, detached).
Never pushed. Temp port patch to `extension/src/background.ts` (8000→8002, needed
because Stream B's backend already occupies port 8000) was **reverted** before
commit — `git status` at commit time shows only the files below.

## 1. Browser investigation — VERDICT: REAL BROWSER + EXTENSION WORKS

A real Chromium (Chrome for Testing 159.0.8154.6) runs in this sandbox with the
real built extension, and the full backend → WebSocket → extension → tab path
works. One end-to-end browser task is **VERIFIED LIVE END-TO-END** (task 2
below). The remaining loop tasks failed for three distinct, evidenced reasons
— none of them is "no browser".

### What was needed to make the browser work (exact commands)

1. Managed system Chrome refuses Playwright ("Hatch WebGL domain policy",
   exit 13) — not fought. Chrome for Testing already present at
   `~/workspace/tools/cft/chrome-linux64/chrome` (`--version` →
   `Google Chrome for Testing 159.0.8154.6`).
2. Plain headless load of `https://example.com` fails:
   `Page load failed: net::ERR_TUNNEL_CONNECTION_FAILED`. Chrome does not
   read the `https_proxy` env var, and `--proxy-server=<full URL with
   credentials>` fails with `net::ERR_NO_SUPPORTED_PROXIES`.
3. Fix: a local unauthenticated forward proxy (`/tmp/e-fwd-proxy.js`, kept
   in /tmp, not committed) that listens on `127.0.0.1:8899` and forwards
   CONNECT with a preemptive `Proxy-Authorization: Basic` header built from
   the `https_proxy` env var (credentials never printed or hardcoded).
   Verified: `curl --proxy http://127.0.0.1:8899 https://example.com/` → 200.
4. With the forwarder, Chrome reached the site but failed TLS:
   `net::ERR_CERT_AUTHORITY_INVALID` (the egress proxy MITMs with its own
   CA; Chrome's NSS db does not trust it). Fix for the test browser:
   `--ignore-certificate-errors`. Then:
   `chrome --headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu
   --ignore-certificate-errors --proxy-server=http://127.0.0.1:8899
   --dump-dom https://example.com` → full `<!DOCTYPE html>…Example Domain`.
5. Extension loading (Playwright `launchPersistentContext`, CFT as
   `executablePath`, `--load-extension=<worktree>/extension/dist`):
   service worker appears as
   `chrome-extension://bbbkiepllnmhlkfiocgcfifbokhnlfbd/background.js`.
   Two probe gotchas, both mine, both documented: (a) `page.setContent()`
   leaves the tab at `about:blank`, where content-script injection is
   refused ("Cannot access contents of url about:blank"); real `http(s)` URLs
   are required. (b) `window.__navigatorAgent` lives in the content
   script's **isolated** world — Playwright `page.evaluate` (main world)
   cannot see it. The faithful path is `chrome.scripting.executeScript`
   funcs issued from the service worker, exactly as `withAgent()` does.
6. Backend on port 8002 (not 8000 — Stream B's backend, pid 22771, already
   listens on 8000; do not kill it). Extension source was temporarily
   patched 8000→8002, rebuilt, tested, then reverted (`git checkout --
   extension/src/background.ts`; the running `dist/` keeps 8002, `dist/` is
   gitignored). Backend DB is per-worktree (`backend/data/privagent.db`).

### Extension ↔ backend relay — VERIFIED

- Backend (own venv `/tmp/e-venv`, `NAVIGATOR_PROVIDER=deepai`,
  `DEEPAI_GATEWAY_BASE_URL=http://127.0.0.1:8452/v1`,
  `DEEPAI_GATEWAY_MODEL=deepseek-v4.1-flash`) reports
  `{"provider":"deepai","baseUrl":"http://127.0.0.1:8452/v1",
  "keyConfigured":true,"model":"deepseek-v4.1-flash"}`.
- After installing the backend local token into `chrome.storage.local`
  (`privagentLocalToken` + `privagentIsolatedProfile: true`) and relaunching,
  `GET /api/isolation/status` → `extensionConnected: true,
  profileVerified: true, extensionProfile: {isolatedProfile: true}`.
- The extension auto-reconnected after a backend restart (backend killed
  and restarted on the DeepAI gateway; status again `extensionConnected:
  true, profileVerified: true` without touching the browser).

### Content-script perception — VERIFIED (probe4, real browser)

Local probe page (`http://127.0.0.1:8931/probe-page.html`, search box,
password + OTP inputs, link, button), driven via the service worker exactly
as the backend relay does:

- `list()` returns 6 elements e0–e5 with role/name/tag/inputType/search/
  bounds. After the role fix: roles are `searchbox, button, textbox,
  textbox, link, button` (link was `a`, inputs were `input` before).
- Click "Plain button" → `{ok:true}`, page title changes to `clicked-plain`.
- Type `hello world` into the search box → `{ok:true, value:"hello world"}`,
  DOM value confirmed `hello world`.
- `pageState()` + `extractText()` return URL/title/textLength/textHash and
  the page text.
- Stale-element recovery: after removing the button from the DOM, `list()`
  re-assigns refs e0–e4 with no gaps; clicking the removed id returns
  `{ok:false, error:"STALE_TARGET"}`.
- Navigation change: re-inject on `https://example.com` → `list()` returns
  the single link `Learn more` → `https://iana.org/help/example-domains`.
- Sensitive values never leak through observation: password input value and
  OTP input value are `null` in `list()` (the walk carries no `value` field;
  `observe()` sends `value: undefined` — see §4).

### Live loop tasks (backend → extension → real browser)

Provider note: FreePi (`http://127.0.0.1:8452`, deepseek-v4.1-flash) was
restarted by this stream (`NODE_USE_ENV_PROXY=1 node src/main.ts` from
`~/workspace/freepi-proxy/`; `/v1/models` lists `deepseek-v4.1-flash`,
direct chat returns model `deepseek-ai/deepseek-v41-flash-1017`).
DeepAI gateway (`~/workspace/deepai-gateway/bin/deepaicode2api`,
`http://127.0.0.1:10087`) was started as the CONTRACTS3 fallback:
`/health` → `status ok, accounts 1, models 28, lane 8`; direct chat
`deepseek-v4.1-flash` returns `DEEPAI-OK`.

| # | Exact instruction (POST /api/task, port 8002) | Result |
|---|---|---|
| 1 | "Go to example.com and tell me what the main link on the page says and where it points to. Do not navigate anywhere else." (allow example.com, iana.org) — task `34e6fd10…` | **FAILED** — `Navigation incomplete: No navigation action was executed or observed.` planner_calls 3, steps_executed 0, 276 ms. Root cause (§5 defect D1): deterministic planner resolves bare domain to a Google search URL, allow-list blocks google.com, loop retries 3× and gives up. The LLM is never consulted. |
| 2 | "Navigate to https://example.com and tell me what the main link on the page says and where it points to." (same allow-list) — task `a68d2a05…` | **VERIFIED LIVE END-TO-END** — state `completed`, 1 step, `navigate → verified`, answer: link **"Learn more"** → **https://iana.org/help/example-domains**. Timings: observe 7 ms, redact 10 ms, plan 2310 ms, execute 2510 ms. |
| 3 | "Navigate to https://en.wikipedia.org and use the search box to search for Chocolate, then tell me the title of the article page that opens." (allow en.wikipedia.org, wikipedia.org, wikimedia.org) — task `983c3bd9…` | **FAILED** — `Loop detected: action 'navigate' repeated on identical page state with zero effect.` 3× navigate, all `completed_unverified`, tab stayed `about:blank`. Root cause (§5 defect D2 + environment): direct headless Chrome load of `https://en.wikipedia.org/wiki/Chocolate` via the same proxy times out at 45 s with empty DOM, while `curl` via the same proxy returns 200 in 1.5 s — Wikipedia does not load in this sandboxed Chrome. The loop correctly detected the loop and stopped instead of hallucinating. |
| 4 | Travel sandbox: "Navigate to http://127.0.0.1:8124/ and search for flights from Delhi to Mumbai, then tell me the cheapest option and its price." (allow 127.0.0.1, localhost) — task `102ce9d9…` (FreePi) | **FAILED** — `Planner unavailable: Planner call failed: HTTP 429 (Rate Limit / Quota Exceeded) — one request at a time, 4,000 credits left today.` Per the 429 hard-stop rule, FreePi was **not** retried; the backend was switched to the DeepAI fallback instead. |
| 5 | Same travel instruction — task `00593371…` (DeepAI) | **FAILED** after 50 steps — `Navigation incomplete: Did not observe target host '127.0.0.1:8124'. Visited: {'127.0.0.1'}.` Root cause (§5 defect D3): the completion verifier compares target host `'127.0.0.1:8124'` (host:port) against visited host `'127.0.0.1'` (no port) — a host-vs-host:port mismatch. The sandbox was almost certainly visited; the verifier rejects it anyway. DeepAI planner calls were observed working (4× `/v1/chat/completions` 200, 3–10 s each, 03:57:45–03:58:12). |
| 6/7 | ShopSphere cart + checkout prep (8125), MailCal browser interaction (8126) | **NOT ATTEMPTED** — time (deadline) plus defect D3 predicts the same host:port verifier failure for every `127.0.0.1:<port>` sandbox URL. Both sandboxes are healthy (`/api/v1/health` 200: shopsphere, mailcal). |

Classification summary: 1 VERIFIED LIVE END-TO-END (task 2), 1 VERIFIED
COMPONENT ONLY (content-script perception/click/type/stale recovery via
probe4), 4 FAILED with root causes (D1, D2, D3, 429), 2 NOT ATTEMPTED.
No real purchases/reservations/payments were made anywhere.

## 2. Privacy + injection — FRESH NUMBERS (2026-11-27, mock provider, offline)

Command (from the worktree root):
`/tmp/e-venv/bin/python backend/scripts/eval_live.py --suite all --date 2026-11-27`
Results: `evals/results/{checker_asr,privacy_leak,mock_benchmark,summary}_2026-11-27.json`.

### Injection checker — ASR 0.0 (was 0.50)

| Metric | 2026-11-16 | 2026-11-27 |
|---|---|---|
| Canonical scan detection | 25/28 (0.8929) | **28/28 (1.0)** |
| Canonical plan flag rate | 21/28 (0.75) | **28/28 (1.0)** |
| Canonical ASR | 0.25 | **0.0** |
| Evasion scan detection | 0/14 (0.0) | **14/14 (1.0)** |
| Evasion plan flag rate | 0/14 (0.0) | **14/14 (1.0)** |
| Evasion ASR | 1.0 | **0.0** |
| Overall ASR | 0.50 | **0.0** |
| Benign false-positive rate | 0/3 | **0/3** |

42/42 attack plans flagged. This is a fixture measurement (45 hand-written
cases), not a claim of comprehensive injection protection — base64/encoded
instructions, instructions split across distant regions, and subtly implied
instructions are still uncaught, and a page merely quoting an attack can
false-positive (the confirmation gate is the backstop).

### Privacy leak — 1/24 (0.0417), unchanged

Same single leak as 2026-11-26: `pii-pwbare-01`, bare unlabelled password
`hunter2-9x7q` in page text. It has no label and no known prefix and is
indistinguishable from an ID/code — redacting it would mean guessing. It
stays a documented known gap (`KNOWN_GAPS` in `test_privacy_leak.py`).
All pattern-covered types leak 0, including bare OTP 0/2, labelled OTP 0/1,
postal address 0/1, phone 0/3. Mock benchmark 8/8.

## 3. Fixes in this stream (file:line in the worktree)

### checker.py — injection hardening (the evasion-ASR fix)

- `backend/app/checker.py` — `_leet_normalize` + two-pass scan in
  `scan_page_text`: raw text plus a leet-decoded copy (`0→o, 1→i, 3→e,
  4→a, 5→s, 7→t, @→a, $→s`) catches `1gn0re`, `Ign0re`, `prev10us`.
- `backend/app/checker.py` — `_spaced()` trigger-verb fragments catch
  split-word `Ign ore`.
- `backend/app/checker.py` — `INJECTION_RULES` broadened: `ignore_instructions`
  allows `the`/`your` (`ignore the above instructions`, `forget your earlier
  directives`), `disregard/forget … told … before`, and `follow_orders` (new
  rule: `follow my orders`, `obey me`, `you work for me now`);
  `reveal_system_prompt` allows filler words (`Show me the hidden
  instructions`); `exfiltrate_secret` adds `forward` + reverse order
  (`password forwarded to …`); `otp_exfiltration` adds `6-digit code` +
  verb-first order (`Paste/Text the verification code …`);
  `credential_harvest` adds `login credentials`/`credentials`;
  `navigate_instruction` matches bare domains (`Go to evil.example/login`);
  `click_instruction` adds `tap`/`press`; `download_and_run` drops the
  `\b` before `.bat`/`.exe`/`.sh` (no word boundary between space and `.`)
  and adds `fetch the setup file`; `disable_security` adds `switch off` /
  `shut off` / `deactivate`.
- `backend/app/checker.py` — `_extract_urls(..., include_bare_domains=True)`
  for navigation findings, so plan rule 2 can match `www.evil.example`
  (fixes the `can-nav-03` plan miss); `_BARE_DOMAIN_RE` fixed to allow
  two-label domains (`evil.example`, was requiring 3 labels).
- `backend/app/checker.py` — `check_plan` rule 6 (new): `click_instruction`
  finding + a click in the plan → flag (closes the documented gap where the
  family scanned 1.0 but flagged 0.0 at plan level); rule 4 now also covers
  `follow_orders`.
- `backend/tests/test_checker_asr.py` — rewritten to the new numbers:
  `KNOWN_SCAN_MISSES` now empty (all three canonical misses fixed),
  evasion asserted with floors (scan ≥ 0.9, plan ≥ 0.9, ASR ≤ 0.1; measured
  1.0/1.0/0.0), `RESULTS_PATH` → `checker_asr_2026-11-27.json`.
- `backend/tests/test_checker.py` — `test_scan_flags_otp_exfiltration_attempt`
  relaxed from exact-set to subset assertion: `Send your OTP` now correctly
  fires both `otp_exfiltration` and `exfiltrate_secret` (reverse-order match).
- `docs/EVALS.md` — all checker + privacy numbers updated to 2026-11-27,
  per-family table rewritten, limitations paragraph updated.

### privacy.py — live OCR leak found and fixed

- `backend/app/privacy.py` — new `PATTERNS["labelled_secret"]`: a label
  (`password|passwd|pwd|secret|api key|auth/access token|client secret`)
  + whitespace/`:`/`=` + an 8+ char token **containing a digit** is redacted
  (label kept, token → `[REDACTED]`), with the matching special case in
  `_redact_text`. The digit requirement keeps `Password requirements` and
  `password authentication required` untouched (verified on 6 sanity cases).
- Found by the new live test, not by reasoning: the first run of
  `test_live_ocr_text_is_redacted_before_the_planner` FAILED with OCR text
  `Password hunter2-9x7q` reaching the planner prompt — `sensitive_kv`
  needs `:`/`=`/`is` between label and value, so a space-separated token
  slipped through. After the fix the test passes.
- `backend/tests/test_ocr_redaction.py` (new) — renders real text into a PNG
  (DejaVuSans 40pt) and runs the real `tesseract` binary: `test_real_tesseract_reads_the_secrets`
  proves the pixels OCR to the OTP + password + address; `test_live_ocr_text_is_redacted_before_the_planner`
  pushes the screenshot through `classify_and_redact` + `build_prompt`
  (with `ALLOW_SCREENSHOTS` + `OCR_ENABLED`) and asserts no secret survives.
  Skipped when no tesseract binary is on PATH. (One transient tesseract
  misread was seen once in a combined run; the test passed on every re-run —
  3 consecutive passes after the fix.)
- `backend/tests/test_privacy_leak.py` — `RESULTS_PATH` →
  `privacy_leak_2026-11-27.json`, docstring notes the 2026-11-27
  re-measurement (still 1/24).

### Extension — observation walk unification (documented + one real fix)

- Verified: the production observation walk is `candidates()` in
  `extension/lib/content.ts` (injected `content.js`, reached via
  `withAgent().list()`); `src/observer.ts::collectInteractiveElements` is
  used ONLY by `extension/tests/observer.test.ts` under jsdom and never runs
  in production. `actionExecutor.ts:12` imports only `sanitizeObservation`
  from it; `observe()` maps `content.js` elements and sends
  `value: undefined` for every element ("Values are never read at
  observation time").
- `extension/lib/content.ts` — added `implicitRole()` (mirrors
  `observer.ts::implicitRole`) and used it as the role fallback in
  `candidates()` instead of the raw tag name. Production roles were `a` /
  `input` / `textarea` / `select`; they are now `link` / `textbox` /
  `searchbox` / `checkbox` / `radio` / `combobox`, matching the backend
  prompt vocabulary (`role="link"`) and `privacy.py`'s link prioritisation.
  Verified live in the real browser (probe4 re-run): roles
  `searchbox, button, textbox, textbox, link, button`.
- `extension/lib/content.ts` (header) + `extension/src/observer.ts` (header)
  — the split is now documented in both files, including that there is no
  automated parity check yet (a change to one copy must be mirrored by hand).

## 4. Verification evidence (tests)

- `backend/scripts/eval_live.py --suite all --date 2026-11-27`: checker ASR
  0.0, privacy 1/24, mock 8/8 (see §2).
- Backend tests (venv `/tmp/e-venv`): `pytest tests/test_checker_asr.py
  tests/test_privacy_leak.py tests/test_privacy_failclosed.py
  tests/test_checker.py tests/test_privacy_status.py` → **41 passed**;
  `tests/test_ocr_redaction.py` → **2 passed** (+ mocked-OCR regression test
  passes). Full suite (`pytest tests -q`): **293 passed, 1 failed** —
  `tests/test_continuous_monitoring.py::test_scheduler_dispatches_task_record_then_detects_change`
  (`assert 0 == 1` monitoring events; the scheduler's HTTP check to
  `https://example.org/product` gets HTTP 404 through the sandbox egress
  proxy, so no change is recorded; fails identically on re-run, 2.95 s).
  Unrelated to this stream's files (monitoring scheduler); needs the owner
  or a reachable check URL.
- Extension tests (`npm test`): **202 passed, 1 failed** —
  `tests/contracts.test.ts > exposes the canonical action type list`:
  received `ACTION_TYPES` contains an extra `"mcp_tool"` vs the test's
  expected list. **Pre-existing at HEAD, verified**: the same test fails
  1/23 in the pristine main clone (`~/workspace/navigator-audit/PRIVAGENT`)
  at `0cae209`. Python `backend/app/contracts.py:27` also lists
  `"mcp_tool"` (Stream B S3-R1 added it to the source but not to the
  extension test's hardcoded list). One-line fix for the owner: add
  `"mcp_tool"` to the expected list in `tests/contracts.test.ts`. Not fixed
  here — `contracts.ts` and its test are outside this stream's files.
- Extension build: `npm run build` clean (background/content/companion).

## 5. Remaining defects (file:line where known)

- **D1 — deterministic planner mis-resolves bare domains** (OUT OF SCOPE
  files, needs owner): `backend/app/planner.py:853` rule 6 calls
  `website_resolver.resolve("example.com")`, which returns canonical URL
  `https://www.google.com/search?q=example.com`
  (`backend/app/website_resolver.py:840`); `plan_from_observation`
  (`backend/app/planner.py`, allow-list check) then raises `PLAN_INVALID`
  because google.com is not allowed, the loop retries 3×, the LLM is never
  consulted, task fails. Workaround verified: phrase navigation with a full
  `https://…` URL (rule 4, `planner.py:723` area) — task 2 passes. Fix shape:
  resolve a well-formed bare domain to `https://<domain>` directly, or fall
  through to the LLM instead of raising on a deterministic-plan allow-list
  miss.
- **D2 — Wikipedia does not load in sandboxed Chrome**: direct headless
  Chrome load of `https://en.wikipedia.org/wiki/Chocolate` via the proxy
  forwarder times out at 45 s with empty DOM; `curl` via the same proxy is
  200 in 1.5 s. Environment/site-specific; example.com loads in ~1–4 s.
  Also a product wart: the navigate action reports `result_ok: true` while
  the tab stays `about:blank` (step status `completed_unverified`) — the
  loop's loop-detector, not the action result, is what stopped the task.
- **D3 — completion verifier host:port mismatch** (OUT OF SCOPE files):
  task 5: `Did not observe target host '127.0.0.1:8124'. Visited:
  {'127.0.0.1'}` after 50 steps. The verifier compares the target
  host:port against visited hostnames without the port. Every
  `127.0.0.1:<port>` sandbox task will hit this. Fix shape: compare
  hostnames (or host:port on both sides) in the completion verifier.
- **429 on FreePi**: one `HTTP 429 (Rate Limit / Quota Exceeded) — one
  request at a time, 4,000 credits left today` hit during task 4. Treated as
  a hard stop for FreePi in that task (not retried); backend switched to the
  DeepAI fallback, which served 4/4 planner calls (200). A later direct
  FreePi ping returned 200, so the 429 was transient/concurrency, but the
  switch stands for the evidence above.
- **Privacy**: bare unlabelled password in page text still leaks 1/24
  (documented gap, no pattern can match it without guessing). Encoded
  (base64) instructions and subtly implied instructions are unmeasured and
  uncaught by the checker.
- **Stale copies citing old numbers** (not fixed — outside this stream's
  files): `backend/app/control_center.py:410` still says "evasion ASR 1.0";
  `docs/ISOLATION.md:160` still cites overall ASR 0.50. Both need a
  one-line update by their owners.

## 6. Commands to start everything this stream touches (for Phase 7)

All localhost-only. Run from the merged repo root unless noted. The
operator token is printed by `backend/scripts/print_local_token.py` (never
committed, never pasted into chat); the extension needs it in
`chrome.storage.local` under `privagentLocalToken` plus
`privagentIsolatedProfile: true`.

```bash
# 1. Egress proxy forwarder for Chrome (this sandbox only; reads https_proxy from env)
node /tmp/e-fwd-proxy.js        # listens 127.0.0.1:8899 (script lives in /tmp — recreate from E-REPORT §1 if lost)
# 2. Providers (either; FreePi first, DeepAI fallback)
cd ~/workspace/freepi-proxy && NODE_USE_ENV_PROXY=1 node src/main.ts   # :8452
cd ~/workspace/deepai-gateway && ./bin/deepaicode2api                  # :10087
# 3. Backend (port 8000 in the merged build; 8002 was used here only because Stream B holds 8000)
cd backend && NAVIGATOR_PROVIDER=deepai DEEPAI_GATEWAY_BASE_URL=http://127.0.0.1:8452/v1 \
  DEEPAI_GATEWAY_MODEL=deepseek-v4.1-flash DEEPAI_GATEWAY_KEY=freepi-local \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
#    (DeepAI fallback: DEEPAI_GATEWAY_BASE_URL=http://127.0.0.1:10087/v1)
# 4. Demo sandboxes
cd demo-sandboxes/travel && python -m uvicorn app:app --host 127.0.0.1 --port 8124
cd demo-sandboxes/ecommerce && python -m uvicorn app:app --host 127.0.0.1 --port 8125
cd demo-sandboxes/mailcal && python -m uvicorn app:app --host 127.0.0.1 --port 8126
# 5. Extension
cd extension && npm install && npm run build   # dist/ is gitignored
#    Load unpacked extension from extension/dist in Chrome for Testing
#    (~/workspace/tools/cft/chrome-linux64/chrome) with flags:
#    --no-sandbox --disable-dev-shm-usage --ignore-certificate-errors
#    --proxy-server=http://127.0.0.1:8899 --load-extension=<abs path>/extension/dist
# 6. Tests
cd backend && python -m pytest tests/test_checker_asr.py tests/test_privacy_leak.py tests/test_ocr_redaction.py -q
cd extension && npm test
```

## 7. What was cut / not done

- ShopSphere cart + checkout prep and MailCal browser interaction via the
  loop: not attempted (time + defect D3 predicts the same host:port
  verifier failure for `127.0.0.1:8125` / `:8126`). Content-script
  click/type is verified at component level; the loop-level sandbox proof
  is owed after D3 is fixed.
- No change to `planner.py`, `website_resolver.py`, `completion_verifier.py`,
  `contracts.ts`, `control_center.py`, or `ISOLATION.md` — outside this
  stream's files; each defect is reported above with file:line for the owner.
- The bare-password 1/24 privacy leak is left as a documented gap (no safe
  pattern exists).
