# CLAUDE.md — sih-26171-landing

NAVIGATOR: SIH 26171 privacy-first browser agent. Vite + vanilla TS + hand CSS, zero runtime deps.

## Commands
- `npm run dev` → http://localhost:5173 (landing) and `/agent.html` (workspace)
- `npm run build` (tsc --noEmit + vite build)
- `npm test` — 25 checks: 10 structure, 8 console, 7 sanitizer (`tests/security.test.mjs`)
- `cd extension && npm test` — 35 Vitest tests; `npx tsc --noEmit`; `npm run build`
- `cd backend && venv\Scripts\python.exe -m pytest tests -q` — 81 tests
- `backend\venv\Scripts\python.exe backend/scripts/benchmark_eval.py` — 8/8 mock harness

## Conventions
- Design tokens live as CSS custom properties in `src/styles/main.css:3`. Never hardcode duplicates.
- Every animation/transition needs a `prefers-reduced-motion` fallback (block at end of `main.css`).
- Fonts: Chakra Petch + IBM Plex Sans only. Never Inter/Roboto/space-grotesk-style defaults.
- All landing sections are in `index.html`; anchors: problem, solution, workflow, features, use-cases, tech, team, cta.
- Hero art: `public/images/hero-space-02.png`, right-masked; the left text column must stay readable.
- Interactive elements require `:focus-visible` styles and work at 390px without horizontal overflow.
- Render untrusted page/model text through `src/sanitize.ts`. Never interpolate raw strings into `innerHTML`.

## Security Invariants
- Privacy redaction is local and fail-closed: any filter error raises `PrivacyViolation`, never a partial payload.
- Screenshots are local-only unless `ALLOW_SCREENSHOTS=true`; sensitive fields always suppress remote images.
- Keep Python `ACTION_TYPES`/risk/timeout rules in parity with `extension/src/contracts.ts` (parity test enforces the lists).
- Backend accepts only loopback and browser-extension origins; WebSockets enforce the same allowlist. Bind to localhost only.
- Plans: max 3 steps, `timeoutMs` 100–60,000, only credential-free `http(s)` navigation.
- Cancellation must reach the running loop; terminal task states must never be rewritten.
- One click per click action; one submit path per typed submit. Do not reintroduce multi-dispatch fallbacks.

## Agents
- `.claude/agents/frontend-builder.md` — implementation (owns src/**, index.html)
- `.claude/agents/ui-auditor.md` — read-only style/layout audit
- `.claude/agents/visual-qa.md` — Chrome DevTools screenshots + console/link checks; writes only to tests/screens/
- `.claude/agents/browser-engineer.md`, `agent-planner.md`, `visual-perception-engineer.md`, `security-auditor.md`, `integration-qa.md`
