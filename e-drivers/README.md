# final4 — Stream E (v4) live-verification evidence + drivers

## Status (2026-11-27 ~22:55 IST): PREP DONE — WAITING FOR MERGE SIGNAL

Streams A–D worktrees (`~/workspace/navigator-audit/worktrees6/{A,B,C,D}`)
are still at base `ab003b9` with no stream commits yet. Per the task, no
final verification run starts until the coordinator signals the merge is
done. This directory holds the prepared drivers; the runs below happen
only on the MERGED commit.

## Runbook (after the merge signal)

0. **Prep the merged tree** (coordinator gives the FINAL commit SHA):
   ```bash
   git -C ~/workspace/navigator-audit/PRIVAGENT worktree add --detach \
     ~/workspace/navigator-audit/final4/backend-copy <FINAL>
   cd ~/workspace/navigator-audit/final4/backend-copy
   git log --oneline -3   # confirm FINAL + merged stream commits
   git status --short     # must be clean
   ```
1. **Fresh venv** (backend requirements only; extension/frontend use npm):
   ```bash
   python3 -m venv ~/workspace/navigator-audit/final4/venv
   ~/workspace/navigator-audit/final4/venv/bin/pip install -r backend-copy/backend/requirements.txt
   ```
2. **Build the extension from the merged copy** (real built extension):
   ```bash
   cd ~/workspace/navigator-audit/final4/backend-copy/extension && npm run build
   # copy dist -> ~/workspace/navigator-audit/final4/extension-dist
   ```
3. **Fresh DB per run series**: `rm -f ~/workspace/navigator-audit/final4/data/privagent.db*`
   (each `run_replay4.py` series gets a fresh DB; delete + restart backend
   between series: orig5, 12step, watches, nonshopping, ambiguous,
   browser-search, price-change).
4. **Start backend + extension**:
   ```bash
   MERGED_COPY=~/workspace/navigator-audit/final4/backend-copy \
     NAV_BACKEND_LOG=~/workspace/navigator-audit/final4/backend-v4.log \
     ./run-backend-freepi4.sh   # port 8000, FreePi deepseek-v4.1-flash
   node ext-launch4.cjs         # real CFT Chrome + built extension
   ```
   Verify FreePi first: `curl -s --noproxy '*' http://127.0.0.1:8452/v1/models`
   must list `deepseek-v4.1-flash`. Do NOT start a second proxy.
5. **Run the series** (one fresh DB each; backend log env `NAV_BACKEND_LOG`
   set so contract lines are counted per step):
   ```bash
   NAV_BACKEND_LOG=~/workspace/navigator-audit/final4/backend-v4.log \
     python3 run_replay4.py prompts-orig5.json orig5-v4
   # ... repeat for prompts-12step.json (12step-v4), prompts-watches.json
   # (watches-v4), prompts-nonshopping.json (nonshopping-v4),
   # prompts-ambiguous.json (ambiguous-v4), prompts-browser-search.json
   # (browser-search-v4) — fresh DB + backend restart between series.
   ```
6. **Price-change probe** (item 4; needs ShopSphere sandbox on 8125):
   ```bash
   cd backend-copy/demo-sandboxes/ecommerce
   # fresh sandbox DB: rm -f ecommerce.db*; then:
   ../../../final4/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8125
   # back in final4/ (fresh NAVIGATOR DB + backend running):
   python3 price_change_probe.py price-change-v4.json --product-query Galaxy
   ```
   Never inject a notification: the probe only changes a seller price via
   the operator endpoint and observes events/outbox/pull/WS-log/chat.
7. **Provider-failure probe** (item 7; scratch backend on port 8001,
   dead endpoint + invalid model; FreePi itself is NOT stopped):
   ```bash
   python3 provider_failure_probe.py provider-failure-v4.json
   ```
8. **Regression** (item 8; merged copy vs pristine `ab003b9`):
   ```bash
   ./run_regression4.sh ~/workspace/navigator-audit/final4/backend-copy \
     ~/workspace/navigator-audit/final4/regression-v4
   ```
9. **Delivery** (after verification, FINAL commit known):
   - Write `~/workspace/navigator-audit/reports/FINAL_DELIVERY4.md`.
   - Stage delivery extras in `staging/`: `start-navigator.bat`,
     `start-navigator.ps1`, `scripts/`, `PROVIDER_SETUP.md`,
     `FINAL_DELIVERY.md`, `FINAL_DELIVERY4.md` (copies; sources are the
     v3 delivery files + the new report — verify no secrets inside).
   - `./build-v4.sh <clean FINAL checkout> ~/workspace/your_files/NAVIGATOR-final-v4.zip`
   - `./make-patch-v4.sh <FINAL> ~/workspace/your_files/NAVIGATOR-patch-v4.diff`
   - Update `~/workspace/navigator-audit/worktrees6/E/E-REPORT.md`, commit
     in the E worktree as `[E-verification] ...`. Do NOT push.

## Files

| File | Purpose |
| ---- | ------- |
| `run_replay4.py` | Chat-replay driver (v3 driver + outbox/events/settings snapshots + per-step contract log-line counts) |
| `prompts-orig5.json` | Item 1: original failed conversation, verbatim |
| `prompts-12step.json` | v3 12-step conversation (regression baseline) |
| `prompts-watches.json` | Item 2: two watches, modify/pause/resume by conversational reference |
| `prompts-nonshopping.json` | Item 3: non-shopping (Wikipedia) page watch |
| `prompts-ambiguous.json` | Item 5: ambiguous instruction + compound watch+schedule |
| `prompts-browser-search.json` | Item 6: browser search, refine destination then date, read results |
| `price_change_probe.py` | Item 4: ShopSphere operator price change → real notification delivery + dedup + reconnect pull |
| `provider_failure_probe.py` | Item 7: dead endpoint + invalid model, honest error, zero mock |
| `run_regression4.sh` | Item 8: full backend/frontend/extension regression, merged vs pristine `ab003b9` |
| `run-backend-freepi4.sh` | Backend launcher (merged copy, fresh DB, FreePi deepseek-v4.1-flash, port 8000) |
| `ext-launch4.cjs` | Real CFT Chrome + built extension launcher |
| `build-v4.sh` | ZIP builder (tracked tree + staging extras, testzip + exclusion + staging checks, SHA-256) |
| `make-patch-v4.sh` | Patch builder (`ab003b9..FINAL`, apply-check on fresh checkout, file-set + tree match) |
| `staging/` | Delivery extras for the ZIP (populated at delivery time) |
| `data/` | Fresh DBs, secrets dir, audit log, `.local_token` (runtime only, never shipped) |

## v3 baseline (from FINAL_DELIVERY3.md — the numbers v4 must beat or match)

- 12-step: **10/12** verified live (steps 4, 5 failed: FreePi 429; sandbox empty page / 50-step budget).
- Original 5-turn: **3/5** (steps 1, 4, 5 verified; steps 2–3 failed, same sandbox limit).
- Unseen: 1 verified / 2 partial / 1 failed.
- Regression on `ab003b9`: backend **1848 passed / 6 failed** (all 6 pre-existing on `bddb272`) / 11 skipped / 6 xpassed; frontend build exit 0; extension build exit 0; vitest **208 passed**.
- Known open gaps v4 streams address: currency/price-metadata browser path (A), semantic dispatch control + conflict (B), browser input verification + search/refine (C), SSRF/generalization (D). Re-verify each against its stream report at merge time.

## Rules (from CONTRACTS6.md)

- Real provider only: FreePi `deepseek-v4.1-flash` at `http://127.0.0.1:8452`. Mock refused, never presented as real.
- Every live claim needs: task id, provider/model, log excerpt or DB row, timestamp.
- Never fabricate prices, identities, release dates. Never inject notifications.
- Three-way separation in every result: LIVE-VERIFIED / IMPLEMENTED BUT UNVERIFIED / REQUIRES USER'S LOCAL CHROME.
- No pushes. No production edits except minimal fixes for failures found during final verification (coordinator approval via the worktree report).
