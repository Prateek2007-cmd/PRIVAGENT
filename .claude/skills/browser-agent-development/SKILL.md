---
name: browser-agent-development
description: Guidelines and patterns for developing the NAVIGATOR browser agent architecture, extension service worker, and FastAPI WebSocket hub.
---

# Browser Agent Development Guide

## Architecture Overview
NAVIGATOR operates across three interconnected processes:
1. **Frontend Landing Page & Console** (`http://localhost:5173`): Captures user tasks, displays real-time progress, and presents confirmation dialogs.
2. **FastAPI Backend Hub** (`http://localhost:8000`): Orchestrates the agent control loop, performs local privacy sanitization, requests plans from a configured Gemini (`gemini-3.8-flash`) or Groq (`openai/gpt-oss-120b`) provider (or the offline mock), and manages confirmation events.
3. **Chrome Manifest V3 Extension**: Programmatically injects content scripts, gathers DOM interactive trees, executes browser actions, and captures screenshots locally (remote vision is opt-in).

## Developing New Browser Actions
1. **Add Contract**: Add action tag to `ACTION_TYPES` in `extension/src/contracts.ts` and `backend/app/contracts.py`. Set appropriate risk floor in `TYPE_ACTION_RISK_FLOOR`.
2. **Action Executor**: Implement execution handler in `extension/src/actionExecutor.ts`.
3. **Planner Vocabulary**: Update system prompt in `backend/app/planner.py` to instruct Gemini on action syntax and verification criteria.
4. **Contract Parity**: Run `test_contracts.py` to ensure zero drift across TypeScript and Python definitions.
