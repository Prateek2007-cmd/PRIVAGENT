import type {
  ActionResult,
  AgentError,
  BrowserAction,
  BrowserObservation,
} from "./contracts";
import { validateAction } from "./contracts";
import { findTarget } from "./targeting";
import { sanitizeObservation } from "./observer";

/** Injected content-script API shape (see lib/content.ts). */
interface AgentElement {
  targetId: string;
  role: string;
  name: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
}

interface AgentApi {
  list: () => Promise<AgentElement[]>;
  click: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  clickCoords: (x: number, y: number) => Promise<{ ok: boolean; targetId?: string; error?: string }>;
  getCoords: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  type: (targetId: string, text: string, clear: boolean, submit: boolean, name?: string) => Promise<{ ok: boolean; error?: string; submitted?: boolean }>;
  scroll: (d: "up" | "down" | "top" | "bottom") => Promise<{ ok: boolean; scrollY: number }>;
}

async function withAgent(tabId: number): Promise<AgentApi> {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"],
  });
  return {
    list: async () => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => (window as unknown as { __navigatorAgent?: { list: () => AgentElement[] } }).__navigatorAgent?.list() ?? [],
      });
      return (r?.result as AgentElement[]) ?? [];
    },
    getCoords: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getCoords: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getCoords(id, nm) ?? null,
        args: [targetId, name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    click: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) => (window as unknown as { __navigatorAgent?: { click: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.click(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    clickCoords: async (x: number, y: number) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (cx: number, cy: number) => (window as unknown as { __navigatorAgent?: { clickCoords: (px: number, py: number) => { ok: boolean; targetId?: string; error?: string } } }).__navigatorAgent?.clickCoords(cx, cy) ?? { ok: false, error: "no agent" },
        args: [x, y],
      });
      return (r?.result as { ok: boolean; targetId?: string; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    type: async (targetId: string, text: string, clear: boolean, submit: boolean, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, val: string, cl: boolean, sub: boolean, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { type: (i: string, v: string, c: boolean, s: boolean, n?: string) => { ok: boolean; error?: string; submitted?: boolean } } }).__navigatorAgent?.type(id, val, cl, sub, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, text, clear, submit, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string; submitted?: boolean }) ?? { ok: false, error: "execution failed" };
    },
    scroll: async (direction: "up" | "down" | "top" | "bottom") => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (dir: string) => (window as unknown as { __navigatorAgent?: { scroll: (d: any) => { ok: boolean; scrollY: number } } }).__navigatorAgent?.scroll(dir as any) ?? { ok: false, scrollY: 0 },
        args: [direction],
      });
      return (r?.result as { ok: boolean; scrollY: number }) ?? { ok: false, scrollY: 0 };
    },
  };
}

async function activeTab(): Promise<number | null> {
  const currentWindowTabs = await chrome.tabs.query({ currentWindow: true });
  // If there is an active web tab (not the console), use it
  const webTabs = currentWindowTabs.filter(t => t.id !== undefined && !isBlockedUrl(t.url ?? "") && !t.url?.includes("localhost:5173"));
  if (webTabs.length > 0) {
    const activeWeb = webTabs.find(t => t.active);
    if (activeWeb?.id !== undefined) return activeWeb.id;
    return webTabs[webTabs.length - 1].id ?? null;
  }
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active?.id !== undefined && !isBlockedUrl(active.url ?? "")) return active.id;
  const all = await chrome.tabs.query({ active: false });
  const candidates = all
    .filter(t => t.id !== undefined && !isBlockedUrl(t.url ?? ""))
    .sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
  return candidates[0]?.id ?? null;
}

async function currentUrl(tabId: number): Promise<string> {
  const tab = await chrome.tabs.get(tabId);
  return tab?.url ?? "";
}

const BLOCKED_SCHEMES = ["chrome://", "chrome-extension://", "file://", "javascript:", "edge://", "about:"];

export function isBlockedUrl(url: string): boolean {
  const u = (url || "").toLowerCase();
  return BLOCKED_SCHEMES.some(s => u.startsWith(s));
}

export async function executeAction(action: BrowserAction, taskId: string): Promise<ActionResult> {
  const v = validateAction(action);
  if (!v.ok) {
    return { ok: false, error: { code: "PLAN_INVALID", message: `invalid action: ${v.error}`, recoverable: false } };
  }
  try {
    switch (action.type) {
      case "navigate": return await doNavigate(action);
      case "click": return await doClick(action, taskId);
      case "type": return await doType(action, taskId);
      case "press": return await doPress(action);
      case "scroll": return await doScroll(action);
      case "zoom": return await doZoom(action);
      case "new_tab": return await doNewTab(action);
      case "select_tab": {
        await chrome.tabs.update(action.params.tabId, { active: true });
        return { ok: true, evidence: `tab ${action.params.tabId} active` };
      }
      case "close_tab": {
        await chrome.tabs.remove(action.params.tabId);
        return { ok: true, evidence: `tab ${action.params.tabId} closed` };
      }
      case "wait": {
        const tabId = await activeTab();
        if (tabId !== null) await ensureTabActive(tabId);
        await new Promise(r => setTimeout(r, Math.min(action.timeoutMs, 10_000)));
        return {
          ok: true,
          observation: tabId !== null ? await observe(tabId) : undefined,
          evidence: "waited for page/app to settle"
        };
      }
      case "observe": {
        const tabId = await activeTab();
        if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
        return { ok: true, observation: await observe(tabId) };
      }
    }
  } catch (e) {
    return err("UNKNOWN", String(e), true);
  }
}

function err(code: AgentError["code"], message: string, recoverable: boolean): ActionResult {
  return { ok: false, error: { code, message, recoverable } };
}

async function doNavigate(action: Extract<BrowserAction, { type: "navigate" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  const p = action.params;
  if (p.direction === "back") { await chrome.tabs.goBack(tabId); }
  else if (p.direction === "forward") { await chrome.tabs.goForward(tabId); }
  else if (p.direction === "reload") { await chrome.tabs.reload(tabId); }
  else if (p.url) {
    if (isBlockedUrl(p.url)) {
      return err("NAVIGATION_FAILED", `blocked scheme: ${p.url}`, false);
    }
    const cur = await chrome.tabs.get(tabId).catch(() => null);
    let targetTabId = tabId;
    if (cur?.url && cur.url.includes("localhost:5173")) {
      const newTab = await chrome.tabs.create({ url: p.url, active: true });
      targetTabId = newTab.id!;
    } else {
      await chrome.tabs.update(tabId, { url: p.url });
    }
    const url = await waitForStable(targetTabId, action.timeoutMs);
    return { ok: true, observation: await observe(targetTabId), evidence: url };
  } else {
    return err("NAVIGATION_FAILED", "navigate needs url or direction", false);
  }
  const url = await waitForStable(tabId, action.timeoutMs);
  return { ok: true, observation: await observe(tabId), evidence: url };
}

async function ensureTabActive(tabId: number): Promise<void> {
  if (typeof chrome === "undefined" || !chrome.tabs) return;
  try {
    await chrome.tabs.update(tabId, { active: true });
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.windowId && chrome.windows) {
      await chrome.windows.update(tab.windowId, { focused: true }).catch(() => null);
    }
  } catch {}
}

async function cdpClick(tabId: number, x: number, y: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  try {
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: Math.round(x),
      y: Math.round(y),
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      buttons: 1,
      clickCount: 1,
      pointerType: "mouse",
    });
    await new Promise(r => setTimeout(r, 60));
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      buttons: 0,
      clickCount: 1,
      pointerType: "mouse",
    });
    await new Promise(r => setTimeout(r, 120));
    return true;
  } catch {
    return false;
  } finally {
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

async function cdpPressKey(tabId: number, key: string): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  try {
    if (key === "Enter") {
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyDown",
        windowsVirtualKeyCode: 13,
        nativeVirtualKeyCode: 13,
        code: "Enter",
        key: "Enter",
        text: "\r",
        unmodifiedText: "\r",
      });
      await new Promise(r => setTimeout(r, 40));
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyUp",
        windowsVirtualKeyCode: 13,
        nativeVirtualKeyCode: 13,
        code: "Enter",
        key: "Enter",
      });
      return true;
    }
    return false;
  } catch {
    return false;
  } finally {
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

async function doClick(action: Extract<BrowserAction, { type: "click" }>, taskId: string): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  let target = findTarget(action.target, cands, "click");
  if (!target && action.target.coords) {
    await cdpClick(tabId, action.target.coords.x, action.target.coords.y);
    const res = await agent.clickCoords(action.target.coords.x, action.target.coords.y);
    if (res.ok) {
      await new Promise(r => setTimeout(r, 600));
      const after = await currentUrl(tabId);
      void taskId;
      return {
        ok: true,
        targetId: res.targetId,
        observation: await observe(tabId),
        evidence: after !== before ? `navigated ${before} → ${after}` : `clicked coords (${action.target.coords.x}, ${action.target.coords.y})`,
      };
    }
  }
  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  // 1. Get exact onscreen coordinates after scrollIntoView
  const coords = await agent.getCoords(targetId, targetName);
  let cdpSuccess = false;
  if (coords && coords.x > 0 && coords.y > 0) {
    // 2. Dispatch hardware-level trusted click via Chrome DevTools Protocol
    cdpSuccess = await cdpClick(tabId, coords.x, coords.y);
  }

  // 3. Dispatch in-page pointer & DOM click only if CDP didn't succeed
  if (!cdpSuccess) {
    const res = await agent.click(targetId, targetName);
    if (!res.ok) {
      return err("STALE_TARGET", res.error ?? `no matching element for ${JSON.stringify(action.target)}`, true);
    }
  }
  await new Promise(r => setTimeout(r, 700));
  const after = await currentUrl(tabId);
  void taskId;
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(tabId),
    evidence: after !== before ? `navigated ${before} → ${after}` : `clicked ${targetName || targetId}`,
  };
}

async function doType(action: Extract<BrowserAction, { type: "type" }>, taskId: string): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "type");
  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  // 1. Ensure target element is focused via hardware click
  const coords = await agent.getCoords(targetId, targetName);
  if (coords && coords.x > 0 && coords.y > 0) {
    await cdpClick(tabId, coords.x, coords.y);
  }

  const res = await agent.type(targetId, action.params.text, action.params.clear ?? true, action.params.submit ?? false, targetName);
  if (!res.ok) return err("STALE_TARGET", res.error ?? "type failed", true);
  if (action.params.submit) {
    // 2. Give React/DOM 120ms to update interactive buttons
    await new Promise(r => setTimeout(r, 120));
    // 3. Refresh candidates to locate onscreen genuine Send or Submit button (never voice note)
    const freshCands = await agent.list();
    const sendBtn = freshCands.find(c =>
      c.enabled && c.visible && (
        c.name.toLowerCase() === "send" ||
        (c.name.toLowerCase().includes("send") && !c.name.toLowerCase().includes("voice") && !c.name.toLowerCase().includes("ptt")) ||
        (c.role === "button" && c.name.toLowerCase().includes("submit") && !c.name.toLowerCase().includes("voice"))
      )
    );
    if (sendBtn) {
      const sendCoords = await agent.getCoords(sendBtn.targetId, sendBtn.name);
      if (sendCoords && sendCoords.x > 0 && sendCoords.y > 0) {
        await cdpClick(tabId, sendCoords.x, sendCoords.y);
      }
    }
    // 4. Also send trusted hardware Enter key via Chrome DevTools Protocol
    await cdpPressKey(tabId, "Enter");
    await new Promise(r => setTimeout(r, 800));
  }
  const after = await currentUrl(tabId);
  void taskId;
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(tabId),
    evidence: res.submitted ? (after !== before ? `searched (${after})` : "typed + submitted") : "typed",
  };
}

async function doPress(action: Extract<BrowserAction, { type: "press" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const key = action.params.key;
  if (key === "Enter") {
    await cdpPressKey(tabId, "Enter");
  }
  await chrome.scripting.executeScript({
    target: { tabId },
    func: (k: string) => {
      const ev = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
      (document.activeElement ?? document.body).dispatchEvent(ev);
      (document.activeElement ?? document.body).dispatchEvent(new KeyboardEvent("keyup", { key: k, bubbles: true }));
    },
    args: [key],
  });
  await new Promise(r => setTimeout(r, 400));
  return { ok: true, observation: await observe(tabId), evidence: `pressed ${key}` };
}

async function doScroll(action: Extract<BrowserAction, { type: "scroll" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const res = await agent.scroll(action.params.direction);
  return { ok: true, observation: await observe(tabId), evidence: `scrollY=${res.scrollY}` };
}

async function doZoom(action: Extract<BrowserAction, { type: "zoom" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  const p = action.params;
  const current = await chrome.tabs.getZoom(tabId);
  let newZoom = current;
  if (p.direction === "in") newZoom = Math.min(current + 0.25, 3.0);
  else if (p.direction === "out") newZoom = Math.max(current - 0.25, 0.25);
  else if (p.direction === "reset") newZoom = 1.0;
  else if (p.factor !== undefined) newZoom = p.factor;
  await chrome.tabs.setZoom(tabId, newZoom);
  const after = await chrome.tabs.getZoom(tabId);
  return { ok: true, observation: await observe(tabId), evidence: `zoom=${after.toFixed(2)}` };
}

async function doNewTab(action: Extract<BrowserAction, { type: "new_tab" }>): Promise<ActionResult> {
  if (isBlockedUrl(action.params.url)) return err("NAVIGATION_FAILED", `blocked scheme: ${action.params.url}`, false);
  const tab = await chrome.tabs.create({ url: action.params.url, active: true });
  await waitForStable(tab.id!, action.timeoutMs);
  return { ok: true, observation: await observe(tab.id!), evidence: `opened ${action.params.url}` };
}

/** Wait until tab status is complete and SPA DOM has interactive elements or settled. */
async function waitForStable(tabId: number, timeoutMs: number): Promise<string> {
  const start = Date.now();
  // 1. Wait for tab network status complete
  for (let i = 0; i < 40; i++) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.status === "complete" && tab.url) break;
    } catch {
      // loading
    }
    if (Date.now() - start > timeoutMs) break;
    await new Promise(r => setTimeout(r, 100));
  }

  // 2. Wait for SPA hydration / DOM elements to render (up to 3.5s)
  const spaStart = Date.now();
  while (Date.now() - spaStart < 3500) {
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          const ready = document.readyState === "complete" || document.readyState === "interactive";
          const els = document.querySelectorAll('button, input, textarea, a[href], [role="button"], [contenteditable], [data-testid], ytd-thumbnail').length;
          const text = document.body?.innerText || "";
          const isConnecting = text.includes("Connecting to WhatsApp") || text.includes("Loading your chats");
          return { ready, els, isConnecting };
        },
      });
      const status = res?.result;
      if (status?.ready && status.els >= 3 && !status.isConnecting) {
        break;
      }
    } catch {}
    await new Promise(r => setTimeout(r, 300));
  }

  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return tab?.url ?? "";
}

async function captureScreenshot(tabId: number): Promise<string | undefined> {
  if (typeof chrome === "undefined" || !chrome.tabs?.captureVisibleTab) return undefined;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!tab.windowId) return undefined;
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 45 });
    return dataUrl;
  } catch {
    return undefined;
  }
}

export async function observe(tabId: number): Promise<BrowserObservation> {
  await ensureTabActive(tabId);
  const tab = await chrome.tabs.get(tabId);
  const agent = await withAgent(tabId);
  let list = await agent.list();

  // If list is empty, wait up to 2.5s polling for SPA elements before returning empty observation
  if (list.length === 0) {
    for (let i = 0; i < 6; i++) {
      await new Promise(r => setTimeout(r, 400));
      list = await agent.list();
      if (list.length > 0) break;
    }
  }

  const [meta] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => ({ y: scrollY, w: innerWidth, h: innerHeight, dpr: devicePixelRatio }),
  });
  const m = meta?.result ?? { y: 0, w: 0, h: 0, dpr: 1 };
  const screenshot = await captureScreenshot(tabId);
  return sanitizeObservation({
    tabId,
    url: tab.url ?? "",
    title: tab.title ?? "",
    interactive: list.map(e => ({
      targetId: e.targetId,
      role: e.role,
      name: e.name,
      tag: e.cssSelector.startsWith("#") ? "id-selected" : "el",
      visible: e.visible,
      enabled: e.enabled,
      value: undefined,
      bounds: e.bounds,
    })),
    scroll: { y: m.y, viewport: { w: m.w, h: m.h } },
    dpr: m.dpr,
    screenshot,
  });
}
