---
name: end-to-end-verification
description: End-to-end testing protocol and benchmark evaluation for representative tasks A through H.
---

# End-to-End Verification Protocol

## Complete Test Battery
Execute all test suites across the stack:
```bash
# 1. Frontend landing page structure & console tests
npm test

# 2. Extension Vitest suite
cd extension && npm test && cd ..

# 3. Backend Pytest suite (34 unit + integration tests)
cd backend && venv\Scripts\python.exe -m pytest tests && cd ..

# 4. Benchmark evaluation suite (Tasks A through H)
backend\venv\Scripts\python.exe backend/scripts/benchmark_eval.py

# 5. Live Gemini API smoke test
backend\venv\Scripts\python.exe backend/scripts/check_gemini.py
```

## Evaluated Benchmark Tasks
- **Test A**: Google search and results summary.
- **Test B**: Laptop spec extraction, scrolling, and tabular comparison.
- **Test C**: Viewport zoom and visual button interaction via coordinates.
- **Test D**: Multi-tab creation, navigation, and cross-tab analysis.
- **Test E**: Contact form data entry without form submission.
- **Test F**: Recovery from stale element targets via re-observation.
- **Test G**: Sanitization of fake credentials and defense against prompt injection.
- **Test H**: Consequential action gating requiring explicit user approval.
