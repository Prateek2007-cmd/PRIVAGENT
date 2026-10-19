# NAVIGATOR — SIH 26171
## On-device Visual Perception for Lightweight Browser Agents

NAVIGATOR is a complete, functional, privacy-first AI browser agent developed for **Smart India Hackathon (SIH 26171)**. It combines a zero-runtime-dependency landing page and task console, a Chrome Manifest V3 extension, a local FastAPI orchestration backend with Gemini (`gemini-3.8-flash`), local fail-closed privacy redaction, spatial bounding-box perception, and automated recovery.

---

## What Actually Works (Verified Capabilities)

| Subsystem | Implementation | Status & Evidence |
|-----------|----------------|-------------------|
| **Landing Interface & Console** | Vite + vanilla TypeScript + hand CSS | ✓ Verified via `npm test` (18/18 checks pass) |
| **Chrome MV3 Extension** | Relay worker + in-page script + action executor | ✓ Verified via Vitest (18/18 tests pass) & production build |
| **Shared Contracts** | TypeScript source mirrored as Pydantic models | ✓ Verified via contract parity test (`test_contracts.py`) |
| **Browser Action Control** | Navigate, click, type, press, scroll, zoom, wait, tabs | ✓ Verified across all 11 action types (`ACTION_TYPES`) |
| **Spatial Perception** | Bounding boxes (`bounds: {x,y,w,h}`), DPR grounding | ✓ Verified via `test_coordinates.py` and Test C |
| **Privacy Redaction** | Strips passwords, tokens, cards, SSNs, API keys locally | ✓ Verified fail-closed via `test_privacy_failclosed.py` |
| **Prompt Injection Defense** | Page text isolated in `<page-context untrusted="true">` | ✓ Verified via `test_planner_injection.py` |
| **Human-in-the-Loop Gate** | Sensitive actions halt for user approval via modal | ✓ Verified via `test_confirmation.py` & Test H |
| **Bounded Agent Loop** | Max 15 steps, 3 retries, replanning on stale elements | ✓ Verified via `test_agentLoop.py` & Test F |
| **Benchmark Scenarios A–H** | All 8 directive test tasks passing | ✓ Verified 100% (8/8) via `benchmark_eval.py` |
| **Live AI Provider** | Gemini REST SDK (`gemini-3.8-flash`) | ✓ Smoke test verified via `check_gemini.py` |

---

## Architecture Overview

```
                      ┌────────────────────────────────┐
                      │    NAVIGATOR Frontend Console   │
                      │    http://localhost:5173       │
                      └───────────────┬────────────────┘
                                      │ REST / WebSocket (/ws/client)
                                      ▼
                      ┌────────────────────────────────┐
                      │    FastAPI Orchestration Hub   │
                      │    http://localhost:8000       │
                      │  - Privacy Redaction Layer      │
                      │  - Gemini Planner Loop         │
                      │  - Confirmation Decision Gate  │
                      └───────────────┬────────────────┘
                                      │ WebSocket (/ws/extension)
                                      ▼
                      ┌────────────────────────────────┐
                      │   Chrome Manifest V3 Extension │
                      │  - Action Executor (all tabs)  │
                      │  - Content Script DOM Observer │
                      │  - Spatial Bounding-Box Mapper │
                      └────────────────────────────────┘
```

---

## Quickstart & Run Instructions

### 1. Start the Frontend
```bash
npm install
npm run dev
# Running on http://localhost:5173
```

### 2. Start the Backend Hub
```bash
cd backend
# Activate Python 3.12 venv
venv\Scripts\activate
# Start Uvicorn server
uvicorn app.main:app --port 8000 --reload
# Running on http://localhost:8000
```
Ensure your Gemini API key is configured in `backend/.env`:
```env
GEMINI_API_KEY=your_gemini_api_key_here
NAVIGATOR_PROVIDER=gemini
```

### 3. Build & Install the Chrome Extension
```bash
cd extension
npm run build
powershell -Command "Copy-Item manifest.json dist/manifest.json"
```
1. Open Google Chrome and navigate to `chrome://extensions`.
2. Toggle on **Developer mode** in the top right.
3. Click **Load unpacked** and select the `sih-26171-landing/extension/dist` folder.
4. The extension service worker connects automatically to `ws://localhost:8000/ws/extension`.

---

## Test & Benchmark Battery

Run all automated test suites:

```bash
# 1. Frontend structure & console tests (18 passed)
npm test

# 2. Extension Vitest suite (18 passed)
cd extension && npm test && cd ..

# 3. Backend Pytest suite (34 passed)
cd backend && venv\Scripts\python.exe -m pytest tests && cd ..

# 4. Benchmark evaluation suite (Tasks A through H)
backend\venv\Scripts\python.exe backend/scripts/benchmark_eval.py

# 5. Live Gemini API smoke test
backend\venv\Scripts\python.exe backend/scripts/check_gemini.py
```

### Benchmark Metrics (Phase 11)
- **Task Completion Rate**: 100.0% (8/8)
- **Correct Action Rate**: 100.0%
- **Target-Selection Errors**: 0
- **Recovery Success Rate**: 100.0% (1/1)
- **Average Scenario Duration**: 45.2 ms
- **Privacy Policy Violations**: 0 (Zero Leaks)
- **Unsupported Action Attempts**: 0

---

## Repository Structure

```
sih-26171-landing/
├── index.html                       # NAVIGATOR landing page + Live Console
├── src/
│   ├── main.ts                      # Navigation, scroll reveals, starfield
│   ├── taskconsole.ts               # Task Console: WebSocket streaming & confirm modal
│   └── styles/                      # Design system, CSS variables, taskconsole.css
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI server, REST & WebSocket routes
│   │   ├── agentLoop.py             # Bounded control loop (observe→plan→act→verify)
│   │   ├── contracts.py             # Mirrored Pydantic schemas (ACTION_TYPES)
│   │   ├── privacy.py               # Local fail-closed PII/credential redactor
│   │   ├── planner.py               # Gemini prompt builder & schema validator
│   │   ├── observation.py           # DPR & scroll coordinate normalizer
│   │   ├── hub.py                   # Task registry & multi-socket WS router
│   │   └── providers/               # Gemini (`google-genai`) & Mock providers
│   ├── tests/                       # 34 tests covering contracts, privacy, loop, A-H
│   └── scripts/
│       ├── check_gemini.py          # Real Gemini provider smoke test
│       └── benchmark_eval.py        # Benchmark suite metrics runner
├── extension/
│   ├── manifest.json                # Chrome MV3 manifest
│   ├── src/
│   │   ├── contracts.ts             # Canonical TypeScript action contracts
│   │   ├── actionExecutor.ts        # Action execution (navigate, click, type, zoom, etc.)
│   │   ├── observer.ts              # DOM element & bounding box collector
│   │   ├── targeting.ts             # Semantic & spatial coordinate resolution
│   │   ├── background.ts            # Extension service worker & WS relay
│   │   └── relay.ts                 # Pure WebSocket routing & framing
│   ├── lib/content.ts               # Programmatically injected in-page agent
│   └── tests/                       # 18 Vitest unit tests
├── .claude/
│   ├── agents/                      # 5 specialist agents (browser, vision, planner, security, qa)
│   └── skills/                      # 5 project-specific skills
└── docs/
    └── ARCHITECTURE.md              # Complete technical architecture specification
```
