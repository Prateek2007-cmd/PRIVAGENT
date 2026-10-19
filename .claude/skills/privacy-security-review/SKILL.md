---
name: privacy-security-review
description: Security auditing protocol for privacy preservation, fail-closed redaction, and prompt injection defenses.
---

# Privacy & Security Review Protocol

## Core Security Tenets
1. **Local-First Sanitization**: Sanitization must execute locally on the host machine before any model API call is dispatched.
2. **Fail-Closed Principle**: If any error, regex fault, or parse failure occurs in `classify_and_redact()`, the operation raises `PrivacyViolation` and stops immediately. Under no circumstances may unvalidated or partially-redacted data be sent to remote endpoints.
3. **Prompt Injection Defense**:
   - Webpage content is enclosed in `<page-context untrusted="true">` tags.
   - The system prompt explicitly frames page content as inert data.
   - All model responses are validated against the strict `TaskPlan` schema.
   - Non-whitelisted action types (e.g. `eval`, `run_js`, `shell`) are rejected with `PLAN_INVALID`.

## Verification Commands
```bash
# Run privacy fail-closed assertions
backend\venv\Scripts\python.exe -m pytest backend/tests/test_privacy_failclosed.py

# Run prompt injection defense assertions
backend\venv\Scripts\python.exe -m pytest backend/tests/test_planner_injection.py
```
