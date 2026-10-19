/**
 * Shared action/observation contracts — the TS source of truth.
 * backend/app/contracts.py mirrors these; a parity test pins drift.
 */

export const ACTION_TYPES = [
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
] as const;

export type ActionType = (typeof ACTION_TYPES)[number];

export const RISK_CLASSES = ["read", "write", "sensitive"] as const;
export type RiskClass = (typeof RISK_CLASSES)[number];

const RISK_ORDER: Record<RiskClass, number> = { read: 0, write: 1, sensitive: 2 };

/** Minimum acceptable risk classification per action type. */
export const TYPE_ACTION_RISK_FLOOR: Record<ActionType, RiskClass> = {
  navigate: "write",
  click: "write",
  type: "write",
  press: "write",
  scroll: "read",
  zoom: "read",
  wait: "read",
  observe: "read",
  select_tab: "read",
  new_tab: "write",
  close_tab: "write",
};

export interface ElementTarget {
  targetId: string;
  /** Semantic anchor — accessible name the observer reported. */
  name?: string;
  /** Fallback CSS selector from the observer registry. */
  cssSelector?: string;
  /** Screenshot-grounded coordinates in CSS pixels (last resort). */
  coords?: { x: number; y: number };
}

interface ActionBase {
  risk: RiskClass;
  /** Verification criteria — checked against a fresh observation. */
  verify: string;
  timeoutMs: number;
}

export interface NavigateAction extends ActionBase {
  type: "navigate";
  params: { url?: string; direction?: "back" | "forward" | "reload" };
}

export interface ClickAction extends ActionBase {
  type: "click";
  target: ElementTarget;
}

export interface TypeAction extends ActionBase {
  type: "type";
  target: ElementTarget;
  params: { text: string; clear?: boolean; submit?: boolean };
}

export interface PressAction extends ActionBase {
  type: "press";
  params: { key: string };
}

export interface ScrollAction extends ActionBase {
  type: "scroll";
  params: { direction: "up" | "down" | "top" | "bottom"; containerId?: string };
}

export interface ZoomAction extends ActionBase {
  type: "zoom";
  params: { factor?: number; direction?: "in" | "out" | "reset" };
}

export interface WaitAction extends ActionBase {
  type: "wait";
  target?: ElementTarget;
}

export interface ObserveAction extends ActionBase {
  type: "observe";
}

export interface SelectTabAction extends ActionBase {
  type: "select_tab";
  params: { tabId: number };
}

export interface NewTabAction extends ActionBase {
  type: "new_tab";
  params: { url: string };
}

export interface CloseTabAction extends ActionBase {
  type: "close_tab";
  params: { tabId: number };
}

export type BrowserAction =
  | NavigateAction
  | ClickAction
  | TypeAction
  | PressAction
  | ScrollAction
  | ZoomAction
  | WaitAction
  | ObserveAction
  | SelectTabAction
  | NewTabAction
  | CloseTabAction;

export interface InteractiveElement {
  targetId: string;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  /** Sensitive fields (password/token) report undefined — never a value. */
  value?: string;
  visible: boolean;
  enabled: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
}

export interface BrowserObservation {
  tabId: number;
  url: string;
  title: string;
  interactive: InteractiveElement[];
  scroll: { y: number; viewport: { w: number; h: number } };
  dpr: number;
  screenshot?: string;
}

export interface ActionResult {
  ok: boolean;
  targetId?: string;
  observation?: BrowserObservation;
  error?: AgentError;
  evidence?: string;
}

export type TaskState =
  | "planning"
  | "awaiting_confirmation"
  | "running"
  | "awaiting_input"
  | "completed"
  | "failed"
  | "cancelled";

export interface TaskProgress {
  taskId: string;
  taskState: TaskState;
  stepsDone: number;
  message: string;
}

export interface PrivacyDecision {
  send: boolean;
  reason: string;
  violations: string[];
}

export interface PermissionRequest {
  kind: "confirmation";
  action: BrowserAction;
  message: string;
}

export interface AgentError {
  code:
    | "STALE_TARGET"
    | "TIMEOUT"
    | "NAVIGATION_FAILED"
    | "PRIVACY_VIOLATION"
    | "PLAN_INVALID"
    | "BUDGET_EXCEEDED"
    | "CANCELLED"
    | "EXTENSION_DISCONNECTED"
    | "UNKNOWN";
  message: string;
  recoverable: boolean;
}

export interface PlannedStep {
  action: BrowserAction;
  why: string;
}

export interface TaskPlan {
  goal: string;
  steps: PlannedStep[];
}

export interface UserTask {
  id: string;
  instruction: string;
  status: TaskState;
}

export function validateAction(
  action: unknown
): { ok: true } | { ok: false; error: string } {
  if (typeof action !== "object" || action === null) {
    return { ok: false, error: "action is not an object" };
  }
  const a = action as Record<string, unknown>;
  const type = a.type as string | undefined;
  if (!type || !ACTION_TYPES.includes(type as ActionType)) {
    return { ok: false, error: `unknown action type: ${String(type)}` };
  }
  const risk = a.risk as RiskClass | undefined;
  if (!risk || !RISK_CLASSES.includes(risk)) {
    return { ok: false, error: `invalid risk class: ${String(risk)}` };
  }
  const floor = TYPE_ACTION_RISK_FLOOR[type as ActionType];
  if (RISK_ORDER[risk] < RISK_ORDER[floor]) {
    return {
      ok: false,
      error: `risk "${risk}" below floor "${floor}" for action "${type}"`,
    };
  }
  if (typeof a.verify !== "string" || a.verify.length === 0) {
    return { ok: false, error: "missing verify criteria" };
  }
  if (typeof a.timeoutMs !== "number" || a.timeoutMs <= 0) {
    if (typeof (a as any).timeout_ms === "number" && (a as any).timeout_ms > 0) {
      a.timeoutMs = (a as any).timeout_ms;
    } else {
      a.timeoutMs = 10000;
    }
  }
  return { ok: true };
}
