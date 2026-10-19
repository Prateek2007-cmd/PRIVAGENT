# CLAUDE.md — sih-26171-landing

NAVIGATOR: SIH 26171 landing page. Vite + vanilla TS + hand CSS, zero runtime deps.

## Commands
- `npm run dev` → http://localhost:5173
- `npm run build` (tsc --noEmit + vite build), `npm test` (structure tests)

## Conventions
- Design tokens live as CSS custom properties in `src/styles/main.css:3`. Never hardcode duplicates.
- Every animation/transition needs a `prefers-reduced-motion` fallback (block at end of `main.css`).
- Fonts: Chakra Petch + IBM Plex Sans only. Never Inter/Roboto/space-grotesk-style defaults.
- All sections are in `index.html`; anchors: problem, solution, workflow, features, use-cases, tech, team, cta.
- Hero art: `public/images/hero-space-02.png`, right-masked; the left text column must stay readable.
- Interactive elements require `:focus-visible` styles and work at 390px without horizontal overflow.

## Agents
- `.claude/agents/frontend-builder.md` — implementation (owns src/**, index.html)
- `.claude/agents/ui-auditor.md` — read-only style/layout audit
- `.claude/agents/visual-qa.md` — Chrome DevTools screenshots + console/link checks; writes only to tests/screens/
