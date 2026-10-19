---
name: accessibility
description: Accessibility requirements for the NAVIGATOR landing page. Use when editing HTML or interactions.
---

# Accessibility Checklist

- Skip link targets `#main`; visible on focus.
- All scroll-reveal text must be visible without JS (`.reveal` only hides when JS is loaded — no JS means opacity 1). Ensure `.no-js` fallback if the CSS is ever changed.
- Animations: every keyframe/transition gated under `@media (prefers-reduced-motion: reduce)` in `main.css`.
- Interactive elements: real `<a href>` / `<button>`, `:focus-visible` outline (2px `--cyan`), keyboard reachable in DOM order.
- Mobile nav button has `aria-expanded` + `aria-controls`; toggled state synced in `main.ts`.
- Contrast: body text `--ink-dim #93a3c9` on `#030616` ≈ 8.9:1 ✓; never dim below this.
- Decorative hero art uses `alt=""` + `aria-hidden`.
