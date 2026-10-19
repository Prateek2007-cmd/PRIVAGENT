---
name: browser-automation-testing
description: Procedures and automated test execution for the extension action executor, observer, and relay using Vitest.
---

# Browser Automation Testing Guide

## Running Extension Tests
Run tests inside `extension/` via Vitest:
```bash
cd extension
npm test
```

## Writing Action Tests
- Use jsdom stubs or pure logic fixtures for DOM element collection (`observer.test.ts`).
- Verify element targeting priorities: `targetId` → `accessible name` → `coordinate bounds` (`targeting.test.ts`).
- Validate action schemas and risk floors against `validateAction()` (`contracts.test.ts`).
- Mock WebSocket connections to test relay reconnection and message routing (`relay.test.ts`).

## Building the Extension for Chrome Unpacked Loading
```bash
cd extension
npm run build
powershell -Command "Copy-Item manifest.json dist/manifest.json"
```
Load the `extension/dist/` folder in `chrome://extensions` with Developer Mode enabled.
