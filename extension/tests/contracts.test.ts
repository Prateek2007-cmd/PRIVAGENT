import { describe, expect, it } from "vitest";
import {
  ACTION_TYPES,
  RISK_CLASSES,
  TYPE_ACTION_RISK_FLOOR,
  validateAction,
  type BrowserAction,
} from "../src/contracts";

describe("contracts", () => {
  it("exposes the canonical action type list", () => {
    expect(ACTION_TYPES).toEqual([
      "navigate",
      "click",
      "type",
      "press",
      "scroll",
      "zoom",
      "wait",
      "observe",
      "select_tab",
      "new_tab",
      "close_tab",
    ]);
  });

  it("exposes the canonical risk classes", () => {
    expect(RISK_CLASSES).toEqual(["read", "write", "sensitive"]);
  });

  it("rejects an action type outside the whitelist", () => {
    const fake = { type: "run_js", risk: "read", timeoutMs: 1000, verify: "url" };
    const result = validateAction(fake as unknown as BrowserAction);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("run_js");
  });

  it("rejects risk below the action's floor (typing is write)", () => {
    const action = {
      type: "type",
      risk: "read",
      timeoutMs: 1000,
      verify: "field filled",
    } as BrowserAction;
    const result = validateAction(action);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("risk");
  });

  it("accepts a valid action", () => {
    const action: BrowserAction = {
      type: "click",
      risk: "write",
      timeoutMs: 5000,
      verify: "page changed",
      target: { targetId: "t1" },
    };
    expect(validateAction(action).ok).toBe(true);
  });

  it("requires verify criteria on every evaluated action", () => {
    const action = { type: "observe", risk: "read", timeoutMs: 1000 } as unknown as BrowserAction;
    const result = validateAction(action);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("verify");
  });

  it("type floor is write", () => {
    expect(TYPE_ACTION_RISK_FLOOR.type).toBe("write");
  });
});
