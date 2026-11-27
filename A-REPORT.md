# STREAM A — Monitoring + Price — REPORT

**Worktree:** `~/workspace/navigator-audit/worktrees3/stream-A`, HEAD `a71f6c2`
`[A-monitoring] streaming large-page fetch, structured prices, generic
new-listings, outbox alert rows` (on top of `0cae209`). No push.
**Status: ALL HARD-BAR ITEMS VERIFIED LIVE.** 21/21 driver checks passed,
0 failed. Nothing cut.

## Verdicts (CONTRACTS3 vocabulary)

| Check | Classification |
|---|---|
| ISRO watch, real site, multiple successful observations, `consecutive_failures` 0 | **VERIFIED LIVE END-TO-END** |
| ISRO new-listings watch via NL (was refused: hardcoded domain list) | **VERIFIED LIVE END-TO-END** |
| ShopSphere Amazfit Bip 5 price change → full 9-point notification chain | **VERIFIED IN REALISTIC SANDBOX END-TO-END** (all 9 points) |
| Amazfit Bip 5 real price via tracker source (pricehistory.app, JSON-LD ₹4,199) | **VERIFIED ON REAL WEBSITE** (tracker page, not Amazon direct) |
| Amazon.in direct price | **BLOCKED** (bot-gate, known from Demo 2; not re-attempted as a price source — the assigned tracker source is the verified one) |

## Fixes (all in the worktree; only my 4 files + tests)

### 1. Hardcoded domain restriction removed — `backend/app/continuous_monitoring_companion.py`
- `:1256-1262` — `_LISTING_DOMAINS` (internshala/linkedin/unstop/amazon/flipkart/croma) deleted; comment records the removal.
- `:1346-1360` (`_extract_tracked_listing`) and `:1066-1074` (`_history_has_monitored_product`) — any pasted http(s) URL is now a candidate.
- `:1378-1395` (`_create_new_listings_watch`) — watchability is now decided by the ONE SSRF guard `change_detector.validate_monitor_url` (same gate the fetch uses), never by an allowlist. Private/internal URLs are still refused honestly (unit-tested: `http://169.254.169.254/...` → "I can't watch …", no mission created).
- The "need the search page" message no longer names Internshala as the only example.

### 2. Bounded streaming + main-content extraction — `backend/app/monitor_check.py`
- `:67-68` — `MAX_STREAM_BYTES = 8_388_608` replaces the 1,048,576-byte hard refusal (`MAX_BODY_BYTES` kept as an alias). Oversize input is **truncated, not refused**.
- `:594+` (`_fetch_once`) — body streamed in 64 KiB chunks and fed to the parser incrementally (the chunk that crosses the cap is still parsed up to the cap — a bug found by my own test: the first version discarded it and returned empty text). `application/json` is now an accepted fetch target for product APIs.
- `:175-200` — `_MAIN_TAGS = {main, article}` (main content wins when present), `_BOILERPLATE_RE` (ad/advert/cookie/popup/newsletter/banner/sidebar/share/social/paywall/subscribe/related/comments by class/id) plus the existing tag skip list. Extracted text capped at 200,000 chars.
- Result carries `used_main_content`, `truncated`, `page_title`, `h1`.
- Live proof: the real ISRO page (1,751,390 B, re-fetched 2026-11-27 03:25 IST) parses in ~1.6 s to 28,418 chars of main content, `truncated=False`, containing `URSC:04:2026` and `Sorry, No Recruitment right now`, with cookie/ad/nav/footer text excluded.

### 3. Product-specific price extraction — `backend/app/monitor_check.py` + `backend/app/change_detector.py`
- `monitor_check.py:462` `extract_product_price` — precedence: JSON-LD Product/Offer (or `AggregateOffer.lowPrice`) > microdata `itemprop=price` > `og:price:amount` / `product:price:amount` meta > price-classed elements. Amounts validated (0 < price ≤ 10,000,000); product name recorded from the same node / `og:title` / h1 for the identity check.
- `monitor_check.py:528` `extract_json_product` — JSON product APIs (ShopSphere): price = validated `lowest_price`, cross-checked against real seller prices (a stale `lowest_price` not matching any seller falls back to the cheapest seller); **a payload with a price but no product name is refused** (identity required).
- `change_detector.py:20-70` `_extract_price` — precedence: structured metadata > price-keyword context ("selling price ₹4,199") > the ONLY currency amount in the text. **Several competing amounts with no structure/context → price None ("Price unavailable"), never the first number.** The old first-currency-match is gone.
- `change_detector.py:140` `_state` — stores `price`, `price_source`, `product_name` in the baseline.
- Live proof: pricehistory.app → ₹4,199.0, source `json-ld`, name `Amazfit Bip 5 Smart Watch with 1.91" Ultra Large Screen, …`; ShopSphere JSON → ₹5,059.0, source `json-api`, name `Amazfit Bip 5`; a fixture page whose first currency number is a ₹999 promo banner still reports ₹4,199.

### 4. False-change suppression — `backend/app/change_detector.py`, `monitor_check.py`
- `change_detector.py` volatile patterns extended: `posted/updated today|yesterday`, `N min read`, standalone `Sponsored`/`Advertisement` markers (in addition to the existing ISO/month-name dates, relative times, counters, clock times).
- Ad/cookie/promo chrome never enters the observation at all (parser skip, fix 2), so it cannot change the hash.
- Unit proof: ad text + timestamp-only changes on a 1.2 MB fixture page → no alert, hash unchanged; a real listing change (`URSC:05:2026`) on the same page → exactly one alert.

### 5. Notification outbox row for monitor alerts — `backend/app/change_detector.py`, `backend/app/monitoring_scheduler.py`
- `change_detector.py:401-414` (`broadcast_event`) — every monitor alert (including death events) now also writes a `notification_outbox` row, kind `monitor`, `ref_id` = event id, in addition to the existing WS `MISSION_EVENT_TRIGGERED` + chat message.
- `monitoring_scheduler.py:214-222` (`list_unread_events`) and `:283-287` (`count_unread_events`) — the unified unread pull/count deduplicate the outbox mirror against its `monitoring_events` row, so one alert is listed and counted exactly once.
- `monitoring_scheduler.py:61-66` docstring updated (the old "outbox holds only non-monitor notifications" line was wrong after this change).

### Tests
- New `backend/tests/test_stream_a_monitoring.py` — **17 tests, all pass**: large-page streaming/truncation, ad/boilerplate exclusion, JSON-LD-beats-banner, JSON product API, competing-amounts → None, contextual price, track-new accepts ISRO, create-watch accepts generic site / refuses private host, noise-no-alert + real-change-alerts, outbox row + pull dedup, ShopSphere below-price chain (5059 → 4799 → one event → no dup).
- Updated 2 assertions in `backend/tests/test_live_monitoring.py` to the intentional new behaviour (JSON is now a supported product feed, so the refusal test now asserts "no product"; oversize now truncates instead of refusing). No other test file touched.
- Suites run in the worktree venv: `test_stream_a_monitoring + test_live_monitoring + test_continuous_monitoring + test_chat_only_monitoring` → **79 passed, 1 failed**. The 1 failure is **pre-existing at HEAD `0cae209`** (verified by running it against the pristine main clone): `test_scheduler_dispatches_task_record_then_detects_change` — the test mocks the browser `run_task`, but with no extension connected the scheduler takes the HTTP path and `https://example.org/product` 404s through this sandbox's proxy. Not caused by this stream; out of scope (touches scheduler dispatch + extension presence, not my files' logic).

## Live verification (isolated backend copy, fresh venv, real provider, no mock)

**Setup.** Backend: isolated copy of the worktree backend at
`~/workspace/navigator-audit/live/stream-A/backend-copy/`, fresh venv
`live/stream-A/venv/` (pip install from `backend/requirements.txt`),
fresh DB `live/stream-A/data/privagent.db`, port **8133**, started with
`live/stream-A/start-backend.sh`. Health:
`{"status":"ok","provider":"antseed","extensionConnected":false,"maxSteps":50}`.
**Provider:** FreePi `http://127.0.0.1:8452` (restarted 2026-11-27 ~03:22 IST,
`NODE_USE_ENV_PROXY=1`), model **`deepseek-v4.1-flash`**,
`NAVIGATOR_PROVIDER=antseed`, `ANTSEED_BASE_URL=http://127.0.0.1:8452/v1`,
dummy key. DeepAI was not needed (FreePi started first try).
**ShopSphere:** fresh copy at `live/stream-A/shopsphere/` (fresh seed DB),
port **8125**, seeded lowest ₹5,059 (PrimeDeals seller id 4, stock 2).
**Driver:** `live/stream-A/driver-A.py`, **580 s, 21/21 checks passed**,
log `live/stream-A/driver-A.log`, result `live/stream-A/result-A.json`.
WS listener: `live/stream-A/ws-A.py` → `ws-A.log` (connected before any
watch was created; independent process).
**Planner calls:** not separately instrumented — 5 NL tasks were sent, each
returned in a single task turn; task ids/states below. No step-count field
is exposed by `GET /api/task/{id}` in this build, so no planner-call number
is claimed.

### Exact prompts, task/mission ids, outcomes
Chat 1 (ISRO + tracker): created 03:36 IST. Chat 2 (ShopSphere): created 03:41 IST.

| # | Exact NL prompt | Task id | Task state | Mission id |
|---|---|---|---|---|
| 0 | `In one short sentence, what is 2+2?` | `20c738d364bc49a68834ba117c31be55` | completed | — |
| 1 | `Track new ISRO recruitment openings on https://www.isro.gov.in/CurrentOpportunities.html and alert me when a new one appears` | `b38ce9b906324f059b284e10df3a7a93` | completed | `350f0721-12d5-41f5-aa12-d5e6cad140b9` (new_listings, "New listings: ISRO recruitment openings") |
| 2 | `Monitor this page https://www.isro.gov.in/CurrentOpportunities.html every 2 minutes and alert me when it changes` | `56e99d5815724e8d95656350f5ac766c` | completed | `3c1d0223-e18a-4cd5-9849-0e2598413b97` (text_change) |
| 3 | `Watch the price of Amazfit Bip 5 at http://127.0.0.1:8125/api/v1/products/1 and alert me if the price drops below ₹5000. Check every 2 minutes.` | `399beb63b64c454bb0a84c06dbd1e621` | **failed** (see wart below — watch works) | `a220df25-142a-4aa2-8b42-f6d636c03159` (below_price, threshold 5000) |
| 4 | `Watch the price of Amazfit Bip 5 at https://pricehistory.app/p/amazfit-bip-5-smart-watch-1-91ultra-CPvEzqmb and alert me if the price drops below ₹4000. Check every 2 minutes.` | `6a33818d5808425790875458a7180ccd` | **failed** (same wart — watch works) | `8fdd4f93-32eb-4e91-86ad-2dc25fb30c5b` (below_price, threshold 4000) |

Provider sanity reply: `2 + 2 equals 4.` (FreePi deepseek-v4.1-flash).

### ISRO — multiple successful observations (hard bar 1)
- New-listings mission `350f0721…` (interval patched 3600 → 120 s via the
  control API after NL creation, S1 precedent — creation itself was NL):
  **2 runs observed, `consecutive_failures: 0`, status `active`**, baseline
  persisted (5,630-char state), `next_run_at` advancing.
- Generic mission `3c1d0223…` (120 s): **2 runs observed,
  `consecutive_failures: 0`, status `active`**, baseline persisted.
- Baseline text starts: `Current Opportunities … View All Current
  Opportunities ICRB DOS IN-SPACe NSIL ANTRIX ISRO HQ HSFC IIST IPRC ISTRAC
  URSC LPSC MCF NARL NRSC …` and contains the dated openings (most recent
  URSC:04:2026, closed 13 Nov 2026 — unchanged from Demo 2 evidence, so no
  alert is correct; both watches are "next opening" watches).
- Final rows (03:46 IST): all four missions `active`,
  `consecutive_failures: 0`.

### ShopSphere — the 9-point chain (hard bar 2)
1. **Original baseline** — first tick 03:41:15 IST: price **₹5,059.0**,
   `price_source: json-api`, `product_name: Amazfit Bip 5`, no event.
2. **Subsequent observation** — after the operator change, next tick
   03:43:10 IST: state price **₹4,799.0**.
3. **Correct threshold evaluation** — event summary:
   `Price below ₹5000 (now ₹4799)`.
4. **Persisted monitoring event** — `monitoring_events` row
   `cf794cf9-3532-4355-947c-bdd46f351ace`, mission
   `a220df25-142a-4aa2-8b42-f6d636c03159`, `detected_at` 1795750990.89,
   `diff_data: {old_price: 5059.0, new_price: 4799.0}`, `notification_sent: 1`.
5. **Notification outbox row** — `notification_outbox`
   `ff6c763b-129a-47f3-80cc-4297d36c43fc`, kind `monitor`,
   `ref_id: cf794cf9-…`, title `Amazfit Bip 5`.
6. **WebSocket delivery** — connected client received
   `{"type":"MISSION_EVENT_TRIGGERED","mission_id":"a220df25-…",
   "title":"Amazfit Bip 5","message":"Price below ₹5000 (now ₹4799)",
   "event_id":"cf794cf9-…","created_at":"2026-11-27T03:43:10Z",…}`
   (in `ws-A.log`).
7. **Chat notification** — `GET /api/chat/{shop-chat}` returns the agent
   message `🔔 Watch alert — Amazfit Bip 5: Price below ₹5000 (now ₹4799)`.
   (Note: chat rows are Fernet-encrypted in SQLite, so a raw `LIKE` on the
   `messages` table finds nothing by design — the API-decrypted read is the
   evidence.)
8. **Reconnect retrieval** — `GET /api/monitoring/events?unread=true`
   returned exactly that event, `unread: 1`; then
   `POST /api/monitoring/events/{id}/read` acknowledged it.
9. **Dedup** — after one further 120 s tick, still exactly **1** event for
   the mission.
- Operator change (independent interface, no injected notification):
  `POST 127.0.0.1:8125/api/v1/operator/price` with
  `X-Operator-Token: demo-operator`, `{"seller_id": 4, "price": 4799}` →
  `{"ok": true, "seller_id": 4, "old_price": 5059, "new_price": 4799}`.

### Tracker source (secondary, real site)
- Mission `8fdd4f93…`: first tick 03:46:01 IST — price **₹4,199.0**,
  `price_source: json-ld`, product
  `Amazfit Bip 5 Smart Watch with 1.91" Ultra Large Screen, Bluetooth
  Calling, Alexa …`, `consecutive_failures: 0`, **no alert** (correct:
  ₹4,199 is above the ₹4,000 threshold).

## Remaining defects / honest warts (not fixed — outside my files or out of scope)
1. **Price-watch NL tasks end `failed` while the watch works.** Tasks #3 and
   #4 above: the reply carries the full "Continuous Price Monitoring
   Activated" confirmation AND the watch is created and runs, but the task
   state is `failed` with prefix `Research incomplete: No substantive
   research sources were examined (search results pages are not sources).`
   Same wart S1 recorded in Demo 2 for the price watch. Root cause is the
   completion verifier in `completion_verifier.py` (not a Stream A file):
   a monitoring-only task trips the research-completeness gate. Do not
   present the task state as the watch state — but the demo should not
   show a "failed" task for a working watch either. Needs a Stream B/C fix.
2. **Generic-watch title is a title-cased URL** (`Page
   Https://Www.Isro.Gov.In/Currentopportunities.Html`) — cosmetic, from
   `_title_from_listing_url`/product-phrase fallback; pre-existing (S1
   noted it too). Not fixed (title derivation lives in the companion but
   the fix is cosmetic and the deadline bar is the observations).
3. **ISRO baseline still contains in-`<main>` chrome** (breadcrumb +
   centre tabs: `Home / Careers / … ICRB DOS …`). It is stable page content
   inside `<main>`, not ads, so it cannot false-alert — but the extracted
   text is noisier than ideal.
4. **Outbox row `delivered_at` stays NULL until the pull/read** — by
   design (delivery is only claimed on WS ack or pull), but a checker that
   reads `delivered_at` alone immediately after the alert will see NULL;
   `notification_sent: 1` on the event row is the live-delivery marker.
5. **Browser-path monitoring unverified** (no extension in this sandbox) —
   all live checks here ran over the HTTP path, as in Demo 2.
6. Amazon.in direct remains bot-gated (Demo 2 finding, unchanged).

## Merge-clean notes for the coordinator
- Files changed: `backend/app/monitor_check.py`, `change_detector.py`,
  `monitoring_scheduler.py`, `continuous_monitoring_companion.py`,
  `backend/tests/test_stream_a_monitoring.py` (new),
  `backend/tests/test_live_monitoring.py` (2 assertions only, see Tests).
  No other files touched; main clone `git status` clean before the worktree
  was created; nothing pushed.
- The outbox change is additive and deduplicated in the unified pull/count,
  so other streams' unread counts are unaffected (verified: pull returns the
  alert exactly once, `unread: 1`).
- The `_state` price change alters baseline hashes (new `price_source` /
  `product_name` fields) — fresh DBs are unaffected; an existing DB's
  watches will re-baseline once (one "changed" evaluation at most, then
  normal dedup). No migration needed.
- **Re-verify (final merged build):** restart FreePi
  (`cd ~/workspace/freepi-proxy && NODE_USE_ENV_PROXY=1 node src/main.ts`),
  start ShopSphere from a fresh copy (`live/stream-A/shopsphere/`,
  `demo2-venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8125`),
  start the backend with `live/stream-A/start-backend.sh` (port 8133,
  token `stream-a-live-token-20261127`), run
  `live/stream-A/venv/bin/python live/stream-A/driver-A.py` (~10 min,
  expects 21/21 PASS). Live processes from this run are still up
  (FreePi :8452, ShopSphere :8125, backend :8133) — stop with
  `pkill -f 'stream-A' ; pkill -f 'uvicorn app:app'` only when the
  coordinator is done with them.
