---
name: browser-testing
description: How to verify this landing page with Chrome DevTools MCP. Use before claiming any UI work complete.
---

# Browser Testing Protocol

1. `npm run dev` in background; note the Vite URL (default http://localhost:5173).
2. chrome-devtools MCP: `new_page`/`navigate_page` → URL.
3. Viewports: `resize_page` to 1650×900, 1440×900, 768×1024, 390×844; reload after each resize.
4. Per viewport: `take_screenshot` → `tests/screens/<name>.png`; `list_console_messages` (zero errors required).
5. Overflow check via `evaluate_script`: `document.documentElement.scrollWidth - innerWidth` must be ≤ 0.
6. Nav audit: click each anchor link, confirm scroll target exists.
7. Screenshots are evidence — a fix is only "done" after re-capture.
