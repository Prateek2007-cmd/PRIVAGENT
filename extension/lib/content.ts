/**
 * Content script injected programmatically by the service worker.
 * Performs in-page interaction and returns serializable results.
 * Exposes window.__navigatorAgent for executeScript probes.
 */
export {}

interface AgentElement {
  targetId: string;
  role: string;
  name: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds: { x: number; y: number; width: number; height: number };
}

interface NavigatorAgent {
  list: () => AgentElement[];
  click: (targetId: string, name?: string) => { ok: boolean; error?: string };
  clickCoords: (x: number, y: number) => { ok: boolean; targetId?: string; error?: string };
  getCoords: (targetId: string, name?: string) => { x: number; y: number; width: number; height: number } | null;
  type: (targetId: string, text: string, clear: boolean, submit: boolean, name?: string) => { ok: boolean; error?: string; submitted?: boolean };
  scroll: (direction: "up" | "down" | "top" | "bottom") => { ok: boolean; scrollY: number };
}

declare global {
  interface Window { __navigatorAgent?: NavigatorAgent }
}

const registry = new Map<string, HTMLElement>();

function cleanId(id: string | undefined): string {
  return (id ?? "").replace(/^#/, "").trim();
}

function queryAllWithShadow(root: Document | ShadowRoot | Element, selector: string): Element[] {
  const list: Element[] = Array.from(root.querySelectorAll(selector));
  try {
    const all = Array.from(root.querySelectorAll("*"));
    for (const el of all) {
      if (el.shadowRoot) {
        list.push(...queryAllWithShadow(el.shadowRoot, selector));
      }
    }
  } catch {}
  return list;
}

function candidates(): AgentElement[] {
  const selectors = [
    'button',
    'input',
    'textarea',
    'select',
    'a[href]',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[role="searchbox"]',
    '[role="menuitem"]',
    '[role="tab"]',
    '[role="option"]',
    '[role="row"]',
    '[role="gridcell"]',
    '[role="listitem"]',
    '[tabindex]',
    '[contenteditable]',
    '[contenteditable="true"]',
    '[data-testid]',
    '[data-tab]',
    'ytd-thumbnail',
    '#video-title',
    '#thumbnail',
    '.ytp-play-button',
    '.ytp-button',
    '[data-icon="send"]',
    '[data-testid="compose-btn-send"]',
    '.ytp-ad-skip-button',
    '.ytp-ad-skip-button-modern',
    '.ytp-skip-ad-button',
    '.ytp-ad-overlay-close-button',
    '[id^="skip-button"]',
    'button[aria-label*="close" i]',
    'button[aria-label*="skip" i]',
    'button.close',
    '[data-testid*="close"]',
    '#onetrust-accept-btn-handler',
    'button[id*="cookie"]',
    'button[id*="accept"]',
  ].join(', ');

  const nodes = queryAllWithShadow(document, selectors);
  const list: AgentElement[] = [];

  nodes.forEach((n, i) => {
    const el = n as HTMLElement;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const isHidden = style.display === "none" || style.visibility === "hidden" || style.opacity === "0";
    const hasSize = r.width > 0 && r.height > 0;
    if (isHidden || !hasSize) return;

    const input = el as HTMLInputElement;
    const rawId = el.id ? el.id.trim() : "";
    const targetId = rawId || `nav-el-${i}`;

    el.setAttribute("data-navigator-id", targetId);
    registry.set(targetId, el);
    if (rawId) {
      registry.set(`nav-el-${i}`, el);
    }

    const isAdSkip = el.matches('.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button, [id^="skip-button"]');
    const isOverlayClose = el.matches('.ytp-ad-overlay-close-button, button[aria-label*="close" i], button.close, [data-testid*="close"]');
    const isVoiceBtn = el.matches('[data-icon="ptt"], [aria-label*="voice" i], [aria-label*="audio" i], [aria-label*="record" i]');
    const isSendBtn = !isVoiceBtn && el.matches('[data-testid="compose-btn-send"], [data-icon="send"], span[data-icon="send"], button[aria-label="Send" i], button[aria-label*="Send message" i]');
    const isComposer = el.matches('footer [contenteditable="true"], [data-tab="10"], [data-testid="conversation-compose-box-input"], footer [role="textbox"]');
    const isCancelVoice = el.matches('[data-icon="cancel"], [data-testid="btn-cancel"], [data-icon="delete-clip"], button[aria-label*="cancel" i]');

    const isWa = window.location.hostname.includes("whatsapp.com");
    const isWaGroupFilter = isWa && el.matches('button, [role="button"]') && el.textContent?.trim() === "Groups";
    const isWaCancelSearch = isWa && el.matches('[data-testid="chat-list-search-esc-button"], [data-icon="x-alt"], button[aria-label*="cancel search" i]');
    const isWaSearch = isWa && el.matches('div[data-tab="3"], [data-testid="chat-list-search"], [aria-label*="search" i]');
    const isWaChatRow = isWa && el.matches('div[role="listitem"], div[role="row"], div[data-testid*="chat-list-item"], div[data-testid="cell-frame-container"]');
    let waChatTitle = "";
    if (isWaChatRow) {
      const titleEl = el.querySelector('[data-testid="cell-frame-title"], span[title], div[title]');
      waChatTitle = titleEl?.getAttribute("title") || titleEl?.textContent?.trim() || "";
    }

    let name = (
      (isAdSkip ? "Skip Ad " : "") +
      (isOverlayClose && !el.textContent?.trim() ? "Close " : "") +
      (el.getAttribute("aria-label") ||
      el.getAttribute("title") ||
      input.placeholder ||
      (el.getAttribute("data-icon") ? `icon ${el.getAttribute("data-icon")}` : "") ||
      (el.getAttribute("data-testid") && !el.textContent?.trim() ? el.getAttribute("data-testid") : "") ||
      el.textContent ||
      input.value ||
      "")
    ).replace(/\s+/g, " ").trim().slice(0, 120);

    if (isAdSkip && !name) name = "Skip Ad";
    if (isOverlayClose && !name) name = "Close";
    if (isVoiceBtn) name = "Voice message";
    if (isCancelVoice) name = "Cancel Recording";
    if (isSendBtn) name = "Send";
    if (isComposer && (!name || name.length > 50)) name = "Type a message";
    if (isWaGroupFilter) name = "Groups";
    if (isWaCancelSearch) name = "Cancel search";
    if (isWaSearch && !name) name = "Search or start new chat";
    if (isWaChatRow && waChatTitle) name = waChatTitle;

    const role = isAdSkip ? "ad-skip" :
                 isOverlayClose ? "close-button" :
                 isComposer ? "textbox" :
                 isWaSearch ? "textbox" :
                 (isSendBtn || isVoiceBtn || isCancelVoice || isWaGroupFilter || isWaCancelSearch) ? "button" :
                 (isWaChatRow ? "listitem" : (el.getAttribute("role") || el.tagName.toLowerCase()));
    const cssSelector = rawId ? `#${CSS.escape(rawId)}` : `[data-navigator-id="${targetId}"]`;

    list.push({
      targetId,
      role,
      name,
      cssSelector,
      enabled: !(input as HTMLButtonElement).disabled && !el.hasAttribute("aria-disabled"),
      visible: true,
      bounds: {
        x: Math.round(r.left),
        y: Math.round(r.top),
        width: Math.round(r.width),
        height: Math.round(r.height),
      },
    });
  });

  return list;
}

function elementFor(targetId: string, name?: string): HTMLElement | null {
  const cid = cleanId(targetId);
  if (cid && registry.has(cid) && registry.get(cid)!.isConnected) {
    return registry.get(cid)!;
  }
  if (targetId && registry.has(targetId) && registry.get(targetId)!.isConnected) {
    return registry.get(targetId)!;
  }

  // 1. Direct query by data-navigator-id
  let el = cid ? (document.querySelector(`[data-navigator-id="${CSS.escape(cid)}"]`) as HTMLElement | null) : null;
  if (el && el.isConnected) return el;

  // 2. Direct query by ID or name attribute
  if (cid) {
    el = document.getElementById(cid);
    if (el && el.isConnected) return el;
    el = document.querySelector(`[name="${CSS.escape(cid)}"]`) as HTMLElement | null;
    if (el && el.isConnected) return el;
  }

  // 3. Search within open Shadow Roots
  const shadowNodes = queryAllWithShadow(document, `[data-navigator-id="${CSS.escape(cid)}"], #${CSS.escape(cid)}`);
  if (shadowNodes.length > 0 && (shadowNodes[0] as HTMLElement).isConnected) {
    return shadowNodes[0] as HTMLElement;
  }

  // 4. Refresh candidates if registry was empty
  candidates();
  if (cid && registry.has(cid) && registry.get(cid)!.isConnected) {
    return registry.get(cid)!;
  }

  // 5. Fallback search by accessible name / textContent
  if (name && name.trim()) {
    const wanted = name.trim().toLowerCase();
    const all = queryAllWithShadow(document, 'button, input, textarea, select, a[href], [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="row"], [role="listitem"], [role="gridcell"], [tabindex], [data-testid], span[title], div[title], ytd-thumbnail, #video-title') as HTMLElement[];

    // Specialized WhatsApp resolution:
    if (window.location.hostname.includes("whatsapp.com")) {
      if (wanted === "groups" || wanted === "group" || wanted.includes("group filter")) {
        const groupsBtn = all.find(e => {
          const t = (e.getAttribute("aria-label") || e.textContent || "").trim().toLowerCase();
          return t === "groups" || t === "group";
        });
        if (groupsBtn) return groupsBtn;
      }
      if (wanted.includes("first group") || wanted.includes("group chat") || wanted === "first chat") {
        const chatRow = document.querySelector('div[role="listitem"], div[role="row"], div[data-testid*="chat-list-item"], div[data-testid="cell-frame-container"]') as HTMLElement | null;
        if (chatRow) return chatRow;
      }
    }

    // Exact or substring match on aria-label or textContent or placeholder
    const matched = all.find(e => {
      const txt = (
        e.getAttribute("aria-label") ||
        e.getAttribute("title") ||
        e.textContent ||
        (e as HTMLInputElement).placeholder ||
        ""
      ).trim().toLowerCase();
      return txt === wanted || txt.includes(wanted) || (txt.length >= 3 && wanted.includes(txt));
    });
    if (matched) return matched;

    // Word token match
    const tokens = wanted.split(/\s+/).filter(t => t.length > 2);
    if (tokens.length > 0) {
      const tokenMatch = all.find(e => {
        const txt = (
          e.getAttribute("aria-label") ||
          e.getAttribute("title") ||
          e.textContent ||
          ""
        ).trim().toLowerCase();
        return tokens.every(tok => txt.includes(tok));
      });
      if (tokenMatch) return tokenMatch;
    }
  }

  return null;
}

window.__navigatorAgent = {
  list: () => {
    registry.clear();
    return candidates();
  },
  getCoords: (targetId, name) => {
    const el = elementFor(targetId, name);
    if (!el) return null;
    try {
      el.scrollIntoView({ block: "center", behavior: "instant" });
    } catch {}
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.left + Math.max(1, r.width / 2)),
      y: Math.round(r.top + Math.max(1, r.height / 2)),
      width: Math.round(r.width),
      height: Math.round(r.height),
    };
  },
  click: (targetId, name) => {
    // Specialized WhatsApp handling: clear search filter before clicking Groups or a chat
    if (window.location.hostname.includes("whatsapp.com")) {
      const nm = (name || "").toLowerCase();
      if (nm === "groups" || nm === "group" || nm.includes("group") || nm.includes("chat")) {
        const cancelSearch = document.querySelector('[data-testid="chat-list-search-esc-button"], [data-icon="x-alt"], button[aria-label*="cancel search" i]') as HTMLElement | null;
        if (cancelSearch && !nm.includes("cancel")) {
          try { (cancelSearch.closest("button") || cancelSearch).click(); } catch {}
        }
      }
    }
    let el = elementFor(targetId, name);
    if (!el) return { ok: false, error: "STALE_TARGET" };
    try {
      el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    } catch {}

    try { (el as HTMLElement).focus?.(); } catch {}

    const r = el.getBoundingClientRect();
    const cx = Math.round(r.left + Math.max(1, r.width / 2));
    const cy = Math.round(r.top + Math.max(1, r.height / 2));

    const downMouse: MouseEventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: cx,
      clientY: cy,
      screenX: cx,
      screenY: cy + 100,
      button: 0,
      buttons: 1,
    };
    const downPointer: PointerEventInit = {
      ...downMouse,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      width: 1,
      height: 1,
    };

    const upMouse: MouseEventInit = {
      ...downMouse,
      buttons: 0,
    };
    const upPointer: PointerEventInit = {
      ...downPointer,
      buttons: 0,
    };
    const clickOpts: MouseEventInit = {
      ...upMouse,
      detail: 1,
    };

    // Innermost leaf element at coordinates (where user's finger/mouse actually touches)
    const leafEl = (document.elementFromPoint(cx, cy) as HTMLElement) || el;
    try { leafEl.focus?.(); } catch {}

    // 1. Dispatch full sequence on the leaf element so it bubbles up naturally to React / framework listeners
    leafEl.dispatchEvent(new PointerEvent("pointerover", downPointer));
    leafEl.dispatchEvent(new PointerEvent("pointerenter", downPointer));
    leafEl.dispatchEvent(new PointerEvent("pointerdown", downPointer));
    leafEl.dispatchEvent(new MouseEvent("mousedown", downMouse));
    leafEl.dispatchEvent(new PointerEvent("pointerup", upPointer));
    leafEl.dispatchEvent(new MouseEvent("mouseup", upMouse));
    leafEl.dispatchEvent(new MouseEvent("click", clickOpts));
    try { leafEl.click(); } catch {}

    // 2. Also dispatch on the target element if distinct from leaf
    if (el !== leafEl) {
      el.dispatchEvent(new PointerEvent("pointerdown", downPointer));
      el.dispatchEvent(new MouseEvent("mousedown", downMouse));
      el.dispatchEvent(new PointerEvent("pointerup", upPointer));
      el.dispatchEvent(new MouseEvent("mouseup", upMouse));
      el.dispatchEvent(new MouseEvent("click", clickOpts));
      try { el.click(); } catch {}
    }

    // 3. If element is wrapped inside a button, link, or clickable row, also trigger the container
    const interactiveContainer = el.closest('button, [role="button"], a[href], [role="row"], [role="listitem"], [role="gridcell"], [data-testid*="cell-frame"], [data-testid*="list-item"]') as HTMLElement | null;
    if (interactiveContainer && interactiveContainer !== el && interactiveContainer !== leafEl) {
      interactiveContainer.dispatchEvent(new MouseEvent("click", clickOpts));
      try { interactiveContainer.click(); } catch {}
    }

    // 4. Anchor navigation follow-through (YouTube, standard links)
    const anchor = (leafEl.tagName.toLowerCase() === "a" ? leafEl :
                   (el.tagName.toLowerCase() === "a" ? el :
                   (leafEl.closest("a") || el.closest("a") || leafEl.querySelector("a") || el.querySelector("a")))) as HTMLAnchorElement | null;
    if (anchor && anchor.href && !anchor.href.startsWith("javascript:")) {
      try { anchor.click(); } catch {}
      if (window.location.hostname.includes("youtube.com") && anchor.pathname.includes("/watch")) {
        window.location.href = anchor.href;
      }
    }

    return { ok: true };
  },
  clickCoords: (x, y) => {
    const el = document.elementFromPoint(x, y) as HTMLElement | null;
    if (!el) return { ok: false, error: "STALE_TARGET" };
    try {
      el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    } catch {}

    const cx = Math.round(x);
    const cy = Math.round(y);

    const downMouse: MouseEventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: cx,
      clientY: cy,
      screenX: cx,
      screenY: cy + 100,
      button: 0,
      buttons: 1,
    };
    const downPointer: PointerEventInit = {
      ...downMouse,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    };
    const upMouse: MouseEventInit = { ...downMouse, buttons: 0 };
    const upPointer: PointerEventInit = { ...downPointer, buttons: 0 };
    const clickOpts: MouseEventInit = { ...upMouse, detail: 1 };

    el.dispatchEvent(new PointerEvent("pointerdown", downPointer));
    el.dispatchEvent(new MouseEvent("mousedown", downMouse));
    el.dispatchEvent(new PointerEvent("pointerup", upPointer));
    el.dispatchEvent(new MouseEvent("mouseup", upMouse));
    el.dispatchEvent(new MouseEvent("click", clickOpts));
    try { el.click(); } catch {}
    return { ok: true, targetId: el.id || undefined };
  },
  type: (targetId, text, clear, submit, name) => {
    let el = elementFor(targetId, name);
    const nm = (name || "").toLowerCase();

    // Specialized WhatsApp Web element resolution:
    if (window.location.hostname.includes("whatsapp.com")) {
      // Discard any active accidental voice note immediately
      const cancelVoice = document.querySelector('[data-icon="cancel"], [data-testid="btn-cancel"], [data-icon="delete-clip"]') as HTMLElement | null;
      if (cancelVoice) {
        try { (cancelVoice.closest("button") || cancelVoice).click(); } catch {}
      }

      if (nm.includes("type") || nm.includes("message") || nm.includes("composer") || !el) {
        const waComposer = document.querySelector('footer [contenteditable="true"], [data-tab="10"], [data-testid="conversation-compose-box-input"]') as HTMLElement | null;
        if (waComposer) el = waComposer;
      } else if (nm.includes("search") || nm.includes("find")) {
        const waSearch = document.querySelector('div[data-tab="3"], [data-testid="chat-list-search"], [aria-label="Search input textbox"]') as HTMLElement | null;
        if (waSearch) el = waSearch;
        if (clear || !text) {
          const cancelSearch = document.querySelector('[data-testid="chat-list-search-esc-button"], [data-icon="x-alt"], [data-icon="back"], button[aria-label*="cancel search" i]') as HTMLElement | null;
          if (cancelSearch) {
            try { (cancelSearch.closest("button") || cancelSearch).click(); } catch {}
          }
        }
      }
    }

    if (el && !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el.isContentEditable)) {
      const inner = el.querySelector("input, textarea, [contenteditable='true'], [contenteditable]") as HTMLElement | null;
      if (inner) el = inner;
    }
    if (!el) {
      if (document.activeElement instanceof HTMLInputElement || document.activeElement instanceof HTMLTextAreaElement || (document.activeElement as HTMLElement)?.isContentEditable) {
        el = document.activeElement as HTMLElement;
      }
    }
    if (!el) return { ok: false, error: "STALE_TARGET" };

    if (el.isContentEditable || el.getAttribute("contenteditable") === "true") {
      el.focus();
      try {
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(el);
        if (clear) {
          sel?.removeAllRanges();
          sel?.addRange(range);
          document.execCommand("delete", false);
        } else {
          range.collapse(false);
          sel?.removeAllRanges();
          sel?.addRange(range);
        }
      } catch {}

      let inserted = false;
      try {
        inserted = document.execCommand("insertText", false, text);
      } catch {}
      if (!inserted || !el.textContent?.includes(text)) {
        el.textContent = (clear ? "" : el.textContent || "") + text;
        el.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText", data: text }));
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      const input = el as HTMLInputElement | HTMLTextAreaElement;
      input.focus();
      if (clear) input.value = "";
      const val = (clear ? "" : input.value) + text;
      const proto = input instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) {
        setter.call(input, val);
      } else {
        input.value = val;
      }
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    if (submit) {
      const keyOpts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
      el.dispatchEvent(new KeyboardEvent("keydown", keyOpts));
      el.dispatchEvent(new KeyboardEvent("keypress", keyOpts));
      el.dispatchEvent(new KeyboardEvent("keyup", keyOpts));

      const clickSend = () => {
        // Discard accidental voice recording if active
        const cancelVoice = document.querySelector('[data-icon="cancel"], [data-testid="btn-cancel"], [data-icon="delete-clip"]') as HTMLElement | null;
        if (cancelVoice) {
          try { (cancelVoice.closest("button") || cancelVoice).click(); } catch {}
          return;
        }

        const sendEl = document.querySelector('[data-testid="compose-btn-send"], [data-icon="send"], span[data-icon="send"], button[aria-label="Send" i], button[aria-label*="Send message" i]') as HTMLElement | null;
        if (sendEl && !sendEl.closest('[data-icon="ptt"], [aria-label*="voice" i], [aria-label*="audio" i]')) {
          const clickable = (sendEl.closest("button") || sendEl) as HTMLElement;
          const r = clickable.getBoundingClientRect();
          if (r.width > 0 && r.height > 0) {
            const pOpts: PointerEventInit = {
              bubbles: true,
              cancelable: true,
              view: window,
              clientX: Math.round(r.left + r.width / 2),
              clientY: Math.round(r.top + r.height / 2),
              pointerId: 1,
              pointerType: "mouse",
              isPrimary: true,
            };
            clickable.focus();
            clickable.dispatchEvent(new PointerEvent("pointerdown", pOpts));
            clickable.dispatchEvent(new MouseEvent("mousedown", pOpts));
            clickable.dispatchEvent(new PointerEvent("pointerup", pOpts));
            clickable.dispatchEvent(new MouseEvent("mouseup", pOpts));
            clickable.click();
          }
        }
      };

      clickSend();
      setTimeout(clickSend, 120);

      // Standard form submit
      const input = el as HTMLInputElement;
      if (input.form) {
        try {
          input.form.requestSubmit();
        } catch {
          try { input.form.submit(); } catch {}
        }
      }

      // YouTube / General submit buttons
      const submitBtn = (input.form?.querySelector('button[type="submit"], input[type="submit"]') ||
                         document.querySelector('#nav-search-submit-button, #search-icon-legacy, input[name="btnK"], button[type="submit"]')) as HTMLElement | null;
      if (submitBtn) {
        try { submitBtn.click(); } catch {}
      }

      return { ok: true, submitted: true };
    }
    return { ok: true };
  },
  scroll: direction => {
    const px = Math.round(innerHeight * 0.8);
    if (direction === "top") scrollTo({ top: 0 });
    else if (direction === "bottom") scrollTo({ top: document.body.scrollHeight });
    else scrollBy({ top: direction === "down" ? px : -px });
    return { ok: true, scrollY: scrollY };
  },
};
