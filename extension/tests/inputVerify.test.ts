/**
 * Stream C (round 6): browser input verification — replace vs append.
 *
 * Failing reproductions first (written before the fix): the old verifier
 * accepted EQUAL-or-ENDS-WITH for non-clear typing, so an append onto a
 * corrupted original still "verified", and replace forgave surrounding
 * whitespace. The contract now is:
 *
 *   replace (clear=true)  -> final DOM value must EQUAL the intended value
 *                            exactly (no trim forgiveness).
 *   append  (clear=false) -> final DOM value must EQUAL
 *                            (original value + appended text) exactly.
 *   unknown original      -> append FAILS honestly (no combined value to
 *                            check against; the planner retries with clear).
 *   echo / mid-string burial -> FAIL in both modes.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cdpKeySequence, executeAction, typedValueMatches } from "../src/actionExecutor";
import type { BrowserAction } from "../src/contracts";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Chrome stub that routes content-script calls by function source. */
function installSetDateChrome(readBackValue: string) {
  const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
    if (injection.files) return [{ result: true }];
    const fnStr = injection.func?.toString() || "";
    if (fnStr.includes("Boolean")) return [{ result: true }];
    if (fnStr.includes("setDate")) return [{ result: { ok: true, value: "2026-12-06" } }];
    if (fnStr.includes("readValue")) return [{ result: { ok: true, value: readBackValue, editable: true } }];
    if (fnStr.includes("list")) {
      return [{
        result: [
          { targetId: "date-1", role: "textbox", name: "Travel date", tag: "input", inputType: "date", enabled: true, visible: true },
        ],
      }];
    }
    if (fnStr.includes("document.readyState")) {
      return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
    }
    if (fnStr.includes("devicePixelRatio")) {
      return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
    }
    return [{ result: { ok: true } }];
  });
  vi.stubGlobal("chrome", {
    tabs: {
      query: vi.fn(async () => [{ id: 1, url: "https://example.com/travel", active: true }]),
      get: vi.fn(async (id: number) => ({ id, url: "https://example.com/travel", title: "Travel", windowId: 1 })),
      update: vi.fn(async () => ({})),
      captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
    },
    scripting: { executeScript },
  });
}

const setDateAction = {
  type: "set_date",
  target: { targetId: "date-1", name: "Travel date" },
  params: { date: "2026-12-06" },
  risk: "write",
  verify: "date set",
  timeoutMs: 5000,
} as BrowserAction;

describe("set_date verifies the exact final value (Stream C round 6)", () => {
  it("passes when the independent read-back equals the intended date", async () => {
    installSetDateChrome("2026-12-06");
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const result = await executeAction(setDateAction, "task-date-1");
    expect(result.ok).toBe(true);
    const line = logSpy.mock.calls.map((c) => String(c[0])).find((s) => s.includes("INPUT_VERIFY"));
    expect(line).toBe(
      "INPUT_VERIFY field=Travel date mode=replace expected=2026-12-06 actual=2026-12-06 result=pass",
    );
  });

  it("fails honestly when the read-back still holds the old date", async () => {
    // The content script claimed ok for 2026-12-06, but the field still
    // holds 2026-12-05 (e.g. a malformed date silently rejected by a
    // date input). The refine must NOT report success.
    installSetDateChrome("2026-12-05");
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const result = await executeAction(setDateAction, "task-date-2");
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("STALE_TARGET");
    const line = logSpy.mock.calls.map((c) => String(c[0])).find((s) => s.includes("INPUT_VERIFY"));
    expect(line).toBe(
      "INPUT_VERIFY field=Travel date mode=replace expected=2026-12-06 actual=2026-12-05 result=fail",
    );
  });
});

describe("typedValueMatches — replace verifies the EXACT final value", () => {
  it("replace passes only on exact equality, no trim forgiveness", () => {
    expect(typedValueMatches("Chennai", "Chennai", true)).toBe(true);
    // Surrounding whitespace is a different final value, not the intended one.
    expect(typedValueMatches(" Chennai ", "Chennai", true)).toBe(false);
    expect(typedValueMatches("Chennai ", "Chennai", true)).toBe(false);
    expect(typedValueMatches("Hyderabad", "Chennai", true)).toBe(false);
  });

  it("replace rejects a value that merely echoes or buries the intended text", () => {
    expect(typedValueMatches("xxChennaiyy", "Chennai", true)).toBe(false);
    expect(typedValueMatches("Search results for Chennai", "Chennai", true)).toBe(false);
    expect(typedValueMatches("ChennaiChennai", "Chennai", true)).toBe(false);
  });
});

describe("typedValueMatches — append verifies the EXPECTED COMBINED value", () => {
  it("append passes when the field holds original + appended text", () => {
    expect(typedValueMatches("Hyderabad to Bengaluru", " to Bengaluru", false, "Hyderabad")).toBe(true);
    // Empty original: combined value is just the typed text.
    expect(typedValueMatches("2026-12-05", "2026-12-05", false, "")).toBe(true);
    expect(typedValueMatches("Bengaluru", "Bengaluru", false, "")).toBe(true);
  });

  it("append fails when the original was corrupted, even though the text ends the value", () => {
    // ENDS-WITH-only verification used to pass both of these: the field
    // holds junk (or a different city) before the appended text, so the
    // submitted value is wrong and must not verify.
    expect(typedValueMatches("XXHyderabad to Bengaluru", " to Bengaluru", false, "Hyderabad")).toBe(false);
    expect(typedValueMatches("Chennai to Bengaluru", " to Bengaluru", false, "Hyderabad")).toBe(false);
  });

  it("append fails on mid-string burial (caret in the middle, not the end)", () => {
    expect(typedValueMatches(" to BengaluruHyderabad", " to Bengaluru", false, "Hyderabad")).toBe(false);
    expect(typedValueMatches("Hyderabad to BenBengalurugaluru", " to Bengaluru", false, "Hyderabad")).toBe(false);
  });

  it("append fails honestly when the pre-typing value is unknown", () => {
    // Without the original there is no combined value to check against;
    // an endsWith guess would let a results-page echo pass as a verified
    // field value. The planner retries with clear=true instead.
    expect(typedValueMatches("Hyderabad to Bengaluru", " to Bengaluru", false)).toBe(false);
    expect(typedValueMatches("Search results for Bengaluru", "Bengaluru", false)).toBe(false);
  });
});

describe("cdpKeySequence — caretEnd moves the caret to the end of the value", () => {
  it("emits an End key with no modifiers", () => {
    const events = cdpKeySequence("caretEnd");

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "End", code: "End",
      windowsVirtualKeyCode: 35, nativeVirtualKeyCode: 35, modifiers: 0,
    });
    expect(events[1]).toMatchObject({ type: "keyUp", key: "End", code: "End" });
  });
});
