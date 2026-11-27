import { queryAllWithShadow, perceptible, rawAccessibleName, semanticElement, stripIconGlyphs, viewportBounds } from "./semantic";
import type { BrowserObservation, InteractiveElement } from "./contracts";

/** DOM walk used ONLY under jsdom in `extension/tests/observer.test.ts`.
 * It is the test mirror of the production walk, `candidates()` in
 * `lib/content.ts` — the walk the agent actually observes through in a
 * real tab. The two must keep the same contract (refs e0.. in list
 * order, bounds, search classification, sensitive-value exclusion) and
 * the same role vocabulary (`implicitRole` below, kept as a small
 * local copy in `lib/content.ts` rather than imported, so the injected
 * bundle does not depend on the observer module — the two copies must
 * stay identical; there is no automated parity check yet, so a change
 * here must be mirrored there by hand). This file never runs in production:
 * values are never read at observation time (`observe()` in
 * actionExecutor.ts sends `value: undefined`); the `value` handling
 * here exists so the jsdom tests can pin the redaction contract for
 * any value that arrives through another path (legacy observations,
 * explicit `readValue` probes). */

const SENSITIVE_INPUT_TYPES = new Set(["password", "hidden"]);
const INTERACTIVE_SELECTORS =
  'button, input, textarea, select, a[href], [role="button"], [role="link"], [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="switch"]';
const MAX_TEXT_LENGTH = 120;

/** DOM walk usable both in-page (content script) and under jsdom in tests. */
export function collectInteractiveElements(
  viewportW: number,
  viewportH: number
): InteractiveElement[] {
  const nodes = queryAllWithShadow(INTERACTIVE_SELECTORS);
  const out: InteractiveElement[] = [];
  for (const node of nodes) {
    const el = node as HTMLElement;
    assertDomNode(el);
    const rect = el.getBoundingClientRect();
    const mapped = viewportBounds(el);
    if (!mapped) continue;
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.bottom < 0 || rect.right < 0 || rect.top > viewportH || rect.left > viewportW) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || !perceptible(el)) continue;
    const input = el as HTMLInputElement;
    const inputType = el.tagName === "INPUT" ? input.type : undefined;
    const sensitive = inputType !== undefined && SENSITIVE_INPUT_TYPES.has(inputType);
    // Task 7: element state for the accessibility tree, and the ref for
    // this observation (position in the returned list — pruned nodes
    // must not leave gaps in the refs).
    const state: { checked?: boolean; selected?: boolean; expanded?: boolean } = {};
    if (el.tagName === "INPUT" && (input.type === "checkbox" || input.type === "radio")) {
      state.checked = Boolean(input.checked);
    } else if (el.getAttribute("aria-checked") !== null) {
      state.checked = el.getAttribute("aria-checked") === "true";
    } else if (el.getAttribute("aria-pressed") !== null) {
      // A toggle button's pressed state is its checked state: without
      // it the planner cannot tell an enabled toggle from a pressed one.
      state.checked = el.getAttribute("aria-pressed") === "true";
    }
    if (el.getAttribute("aria-selected") !== null) {
      state.selected = el.getAttribute("aria-selected") === "true";
    }
    if (el.getAttribute("aria-expanded") !== null) {
      state.expanded = el.getAttribute("aria-expanded") === "true";
    }
    const rawName = rawAccessibleName(el);
    const name = stripIconGlyphs(rawName);
    // Stream F (generalisation): role computed once and reused, so the
    // search flag below and the reported role can never disagree.
    const role = explicitRole(el) || implicitRole(el);
    // A link's destination is generic, planner-useful metadata: on an
    // unfamiliar site it tells the planner where "Details" or "Select"
    // actually goes before it clicks.
    const href = el.tagName === "A" ? (el as HTMLAnchorElement).href || undefined : undefined;
    out.push({
      targetId: el.id || "",
      elementRef: `e${out.length}`,
      semantic: semanticElement(el, el.id || "") || undefined,
      role,
      name,
      // Debugging aid only: the pre-strip name, kept when it differs.
      ...(rawName !== name ? { rawName } : {}),
      tag: el.tagName.toLowerCase(),
      inputType,
      // Explicit search semantic (parity with the content-script walk):
      // a searchbox is discoverable as "the search field" on any site,
      // not only on sites whose search input happens to be named Search.
      search: inputType === "search" || role === "searchbox" ? true : undefined,
      href,
      value: sensitive ? undefined : trim(input?.value),
      visible: true,
      // aria-disabled is a real disabled state even when the DOM
      // `disabled` property is false (custom controls, links).
      enabled: !(el as HTMLButtonElement).disabled && el.getAttribute("aria-disabled") !== "true",
      bounds: mapped,
      state: Object.keys(state).length > 0 ? state : undefined,
    });
  }
  return out;
}

function assertDomNode(el: unknown): asserts el is HTMLElement {
  if (typeof Element === "undefined" || !(el instanceof Element)) {
    throw new Error("observer used outside a DOM document");
  }
}

function explicitRole(el: Element): string {
  const r = el.getAttribute("role");
  return r && r.length > 0 ? r : "";
}

function implicitRole(el: Element): string {
  switch (el.tagName.toLowerCase()) {
    case "button": return "button";
    case "a": return "link";
    case "input": {
      const t = (el as HTMLInputElement).type;
      if (t === "search") return "searchbox";
      if (t === "text" || t === "email" || t === "url" || t === "password") return "textbox";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      return "textbox";
    }
    case "textarea": return "textbox";
    case "select": return "combobox";
    default: return "generic";
  }
}

function trim(s: string | null | undefined): string | undefined {
  if (s === null || s === undefined) return undefined;
  const t = s.replace(/\s+/g, " ").trim();
  return t.length === 0 ? undefined : t.slice(0, MAX_TEXT_LENGTH);
}

/** Post-walk sanitizer. Fail-closed: re-strips every sensitive-typed value,
 * even if the walk somehow reported one. */
export function sanitizeObservation(obs: BrowserObservation): BrowserObservation {
  const interactive = obs.interactive.map(e =>
    e.inputType !== undefined && SENSITIVE_INPUT_TYPES.has(e.inputType)
      ? { ...e, value: undefined }
      : e
  );
  return { ...obs, interactive };
}
