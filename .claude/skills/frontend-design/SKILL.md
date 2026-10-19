---
name: frontend-design
description: Design system for the NAVIGATOR SIH 26171 landing page. Use when adding sections or styling.
---

# NAVIGATOR Design System

- **Aesthetic**: mission-control OS. Dark navy cosmos, glass panels, cyan/violet illumination.
- **Fonts**: Chakra Petch (display, uppercase kickers, buttons) + IBM Plex Sans (body). Never substitute Inter/Roboto.
- **Palette** (custom properties in `src/styles/main.css`): `--bg #030616`, `--bg-2 #060b24`, `--ink #e8eeff`, `--ink-dim #93a3c9`, `--cyan #46d7ff`, `--violet #a06bff`.
- **Surfaces**: glass cards `rgba(10,18,48,.55)` + 1px `--line` border + 18px radius. Hover = lift + cyan border.
- **Gradient text**: `.grad` utility. Gradient buttons `--grad` background with dark text.
- **Motion**: staggered hero reveal on load, scroll reveals via `.reveal`/`.is-visible`, counters on intersect. All gated by `prefers-reduced-motion`.
- **Hero art**: `hero-space-02.png` right-masked; text column must stay readable. Never flatten interactive elements to images.
