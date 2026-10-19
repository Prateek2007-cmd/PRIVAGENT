import "./styles/taskconsole.css";

const API = "http://localhost:8000";

const input = document.getElementById("taskInput") as HTMLTextAreaElement | null;
const startBtn = document.getElementById("taskStart") as HTMLButtonElement | null;
const cancelBtn = document.getElementById("taskCancel") as HTMLButtonElement | null;
const stateEl = document.getElementById("consoleState") as HTMLElement | null;
const stepsEl = document.getElementById("taskSteps") as HTMLOListElement | null;
const summaryEl = document.getElementById("taskSummary") as HTMLElement | null;
const confirmModal = document.getElementById("confirmModal") as HTMLElement | null;
const confirmMsg = document.getElementById("confirmMsg") as HTMLElement | null;
const confirmAction = document.getElementById("confirmAction") as HTMLElement | null;
const approveBtn = document.getElementById("confirmApprove") as HTMLButtonElement | null;
const rejectBtn = document.getElementById("confirmReject") as HTMLButtonElement | null;

if (confirmModal) confirmModal.hidden = true;
if (cancelBtn) cancelBtn.hidden = true;
if (summaryEl) summaryEl.hidden = true;

let currentTaskId: string | null = null;
let confirmResolve: ((decision: boolean) => void) | null = null;

function setState(text: string): void {
  if (stateEl) stateEl.textContent = text;
}

function escapeHtml(text: string): string {
  const d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}

function formatSummary(text: string): string {
  let escaped = escapeHtml(text);
  escaped = escaped.replace(/(https?:\/\/[^\s&<)]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer" style="color: #46d7ff; text-decoration: underline; font-weight: 500;">$1 ↗</a>');
  return escaped;
}

function actionIcon(msg: string): string {
  const m = msg.toLowerCase();
  if (m.includes("navigate") || m.includes("opened") || m.includes("http")) return "🧭";
  if (m.includes("click")) return "🖱️";
  if (m.includes("type") || m.includes("enter") || m.includes("submit") || m.includes("searched")) return "⌨️";
  if (m.includes("scroll")) return "📜";
  if (m.includes("zoom")) return "🔍";
  if (m.includes("wait")) return "⏳";
  if (m.includes("sign-in") || m.includes("login") || m.includes("sensitive") || m.includes("credential")) return "🔐";
  return "⚡";
}

function renderTaskState(taskState: string, message: string): void {
  // Single source of truth: the backend task state. The UI never invents "complete".
  setState(taskState);
  if (taskState === "completed" && summaryEl) {
    summaryEl.innerHTML = `<strong>✓ Completed</strong> · ${formatSummary(message || "Task completed.")}`;
    summaryEl.hidden = false;
  } else if (taskState === "failed" && summaryEl) {
    summaryEl.innerHTML = `<strong>✗ Blocked / Failed</strong> · ${formatSummary(message || "Task stopped.")}`;
    summaryEl.hidden = false;
  } else if (summaryEl) {
    summaryEl.hidden = true;
  }
}

function addStep(message: string): void {
  if (!stepsEl) return;
  const li = document.createElement("li");
  li.innerHTML = `<span style="opacity: 0.9; margin-right: 4px;">${actionIcon(message)}</span> ${formatSummary(message)}`;
  stepsEl.appendChild(li);
}

function openConfirm(message: string, action: unknown): Promise<boolean> {
  return new Promise(resolve => {
    if (!confirmModal) { resolve(false); return; }
    confirmResolve = resolve;
    if (confirmMsg) confirmMsg.textContent = message;
    if (confirmAction) confirmAction.textContent = JSON.stringify(action, null, 2);
    confirmModal.hidden = false;
  });
}

function closeConfirm(decision: boolean): void {
  if (confirmModal) confirmModal.hidden = true;
  confirmResolve?.(decision);
  confirmResolve = null;
}

approveBtn?.addEventListener("click", () => {
  const id = currentTaskId;
  closeConfirm(true);
  if (id) fetch(`${API}/api/task/${id}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "approve" }) });
});
rejectBtn?.addEventListener("click", () => {
  const id = currentTaskId;
  closeConfirm(false);
  if (id) fetch(`${API}/api/task/${id}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "reject" }) });
});

input?.addEventListener("input", () => {
  if (startBtn) startBtn.disabled = !input.value.trim();
});

startBtn?.addEventListener("click", async () => {
  if (!input || !startBtn || !input.value.trim()) return;
  startBtn.disabled = true;
  if (stepsEl) stepsEl.innerHTML = "";
  if (summaryEl) summaryEl.hidden = true;
  setState("planning");
  try {
    const res = await fetch(`${API}/api/task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction: input.value.trim() }),
    });
    if (!res.ok) throw new Error(`backend ${res.status}`);
    const data = (await res.json()) as { taskId: string };
    currentTaskId = data.taskId;
    if (cancelBtn) cancelBtn.hidden = false;

    try {
      const stRes = await fetch(`${API}/api/task/${currentTaskId}`);
      if (stRes.ok) {
        const t = (await stRes.json()) as { taskState: string; message: string };
        if (t.taskState && t.taskState !== "planning") {
          renderTaskState(t.taskState, t.message);
          if (["completed", "failed", "cancelled"].includes(t.taskState)) {
            startBtn.disabled = false;
            if (cancelBtn) cancelBtn.hidden = true;
          }
        }
      }
    } catch { /* initial poll ignore */ }
  } catch (e) {
    setState(`backend offline — start uvicorn on :8000 (${e})`);
    startBtn.disabled = false;
  }
});

cancelBtn?.addEventListener("click", async () => {
  if (!currentTaskId) return;
  await fetch(`${API}/api/task/${currentTaskId}/cancel`, { method: "POST" });
  if (cancelBtn) cancelBtn.hidden = true;
});

function connectEvents(): void {
  const ws = new WebSocket(`ws://localhost:8000/ws/client`);
  ws.onmessage = ev => {
    try {
      const env = JSON.parse(ev.data) as { type: string; taskId: string; payload: { taskState: string; stepsDone: number; message: string } };
      if (env.type !== "PROGRESS" || env.taskId !== currentTaskId) return;
      const p = env.payload;
      renderTaskState(p.taskState, p.message);
      addStep(`[${p.stepsDone}] ${p.message}`);
      if (p.taskState === "awaiting_confirmation") {
        fetch(`${API}/api/task/${env.taskId}`)
          .then(r => r.json())
          .then(t => openConfirm(t.pending_confirmation?.message ?? "Confirm this action", t.pending_confirmation?.action ?? {}));
      }
      if (["completed", "failed", "cancelled"].includes(p.taskState)) {
        startBtn && (startBtn.disabled = false);
        if (cancelBtn) cancelBtn.hidden = true;
      }
    } catch { /* non-JSON frame — ignore */ }
  };
  ws.onclose = () => setTimeout(connectEvents, 3000);
}

if (startBtn && stateEl) connectEvents();
