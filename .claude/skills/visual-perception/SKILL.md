---
name: visual-perception
description: Guidelines for DOM observation, bounding box extraction, DPR coordinate normalization, and visual element grounding.
---

# Visual Perception Skill

## Element Bounding Boxes
Interactive elements collected in `extension/src/observer.ts` and `extension/lib/content.ts` extract viewport-relative bounding boxes via `getBoundingClientRect()`:
```ts
bounds: {
  x: Math.round(rect.left),
  y: Math.round(rect.top),
  width: Math.round(rect.width),
  height: Math.round(rect.height),
}
```

## Coordinate Grounding & DPR Translation
- Page interactions occur in CSS pixels.
- Screenshots captured via Chrome DevTools/debugger operate in physical device pixels.
- Grounding normalization in `backend/app/observation.py`:
```python
def ground_coords(*, device_x: float, device_y: float, dpr: float, scroll_y: float) -> GroundedPoint:
    return GroundedPoint(x=device_x / dpr, y=device_y / dpr + scroll_y)
```

## Target Resolution Priority
1. `targetId`: Exact matching against active DOM element IDs.
2. `name`: Semantic accessible name or aria-label matching.
3. `coords`: Bounding-box intersection and `elementFromPoint(x, y)` fallback.
