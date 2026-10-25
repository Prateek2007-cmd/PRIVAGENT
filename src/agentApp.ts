import "./styles/agent.css";
import { escapeHtml, linkify, linkLabel, safeHttpUrl, safeImageSrc } from "./sanitize";

const API = "http://localhost:8000";

// DOM Elements
const missionInput = document.getElementById("missionInput") as HTMLTextAreaElement | null;
const btnStartMission = document.getElementById("btnStartMission") as HTMLButtonElement | null;
const btnCancelMission = document.getElementById("btnCancelMission") as HTMLButtonElement | null;
const btnNewSession = document.getElementById("btnNewSession") as HTMLButtonElement | null;
const stateBadge = document.getElementById("missionStateBadge") as HTMLElement | null;
const stepsList = document.getElementById("agentStepsList") as HTMLOListElement | null;
const stepsCount = document.getElementById("feedStepsCount") as HTMLElement | null;
const resultCard = document.getElementById("agentResultCard") as HTMLElement | null;
const resultContent = document.getElementById("resultContent") as HTMLElement | null;
const resultBadge = document.getElementById("resultBadge") as HTMLElement | null;
const resultTimestamp = document.getElementById("resultTimestamp") as HTMLElement | null;

// Human Gate
const humanGate = document.getElementById("humanGate") as HTMLElement | null;
const humanGateTitle = document.getElementById("humanGateTitle") as HTMLElement | null;
const humanGateMsg = document.getElementById("humanGateMsg") as HTMLElement | null;
const btnGateApprove = document.getElementById("btnGateApprove") as HTMLButtonElement | null;
const btnGateReject = document.getElementById("btnGateReject") as HTMLButtonElement | null;

// Inspector
const inspectorUrl = document.getElementById("inspectorUrl") as HTMLAnchorElement | null;
const inspectorTitle = document.getElementById("inspectorTitle") as HTMLElement | null;
const inspectorEvidence = document.getElementById("inspectorEvidence") as HTMLElement | null;
const telStepsDone = document.getElementById("telStepsDone") as HTMLElement | null;
const telPlannerLatency = document.getElementById("telPlannerLatency") as HTMLElement | null;
const obstacleBadge = document.getElementById("obstacleBadge") as HTMLElement | null;
const inspectorScreenshot = document.getElementById("inspectorScreenshot") as HTMLImageElement | null;
const inspectorScreenshotPlaceholder = document.getElementById("inspectorScreenshotPlaceholder") as HTMLElement | null;

// WS status
const wsDot = document.getElementById("wsDot") as HTMLElement | null;
const wsText = document.getElementById("wsText") as HTMLElement | null;

let currentTaskId: string | null = null;
let stepCounter = 0;
let cancelRequested = false;

function setConnectionStatus(online: boolean, text: string): void {
  if (wsDot) {
    wsDot.className = online ? "status-dot status-dot--online" : "status-dot status-dot--offline";
  }
  if (wsText) wsText.textContent = text;
}

function setTaskState(state: string): void {
  if (!stateBadge) return;
  stateBadge.textContent = state.toUpperCase();
  if (["running", "planning"].includes(state.toLowerCase())) {
    stateBadge.style.color = "#38bdf8";
    stateBadge.style.background = "rgba(56, 189, 248, 0.2)";
  } else if (state === "completed") {
    stateBadge.style.color = "#34d399";
    stateBadge.style.background = "rgba(52, 211, 153, 0.2)";
  } else if (["failed", "cancelled"].includes(state)) {
    stateBadge.style.color = "#f87171";
    stateBadge.style.background = "rgba(248, 113, 113, 0.2)";
  } else {
    stateBadge.style.color = "";
    stateBadge.style.background = "";
  }
}

function updateVisualFrame(screenshot: string | null | undefined, message: string, taskState: string): void {
  const m = message.toLowerCase();
  // Word boundaries matter: "ready"/"loading" contain "ad" as a substring.
  const hasObstacle = /\b(ad|ads|advert\w*|skip|popup|modal|cookie|banner|dismiss|overlay)\b/.test(m);

  if (obstacleBadge) {
    if (taskState === "awaiting_confirmation" || m.includes("sign-in") || m.includes("login") || m.includes("password") || m.includes("payment")) {
      obstacleBadge.className = "obstacle-badge obstacle-badge--shielded";
      obstacleBadge.textContent = "🔒 Shielded (Auth/Payment)";
    } else if (hasObstacle) {
      obstacleBadge.className = "obstacle-badge obstacle-badge--detected";
      obstacleBadge.textContent = "⚠️ Obstacle Detected (Ad/Modal)";
    } else {
      obstacleBadge.className = "obstacle-badge obstacle-badge--clear";
      obstacleBadge.textContent = "✓ Workflow Clear";
    }
  }

  const dataUrl = safeImageSrc(screenshot);
  if (dataUrl && inspectorScreenshot) {
    inspectorScreenshot.src = dataUrl;
    inspectorScreenshot.hidden = false;
    if (inspectorScreenshotPlaceholder) inspectorScreenshotPlaceholder.hidden = true;
  }
}

function actionIcon(msg: string): string {
  const m = msg.toLowerCase();
  if (m.includes("navigate") || m.includes("opened") || m.includes("http")) return "🧭";
  if (m.includes("click")) return "🖱️";
  if (m.includes("type") || m.includes("searched") || m.includes("submit")) return "⌨️";
  if (m.includes("scroll")) return "📜";
  if (m.includes("zoom")) return "🔍";
  if (m.includes("wait")) return "⏳";
  if (m.includes("sign-in") || m.includes("login") || m.includes("sensitive") || m.includes("credential")) return "🔐";
  return "⚡";
}

function formatResultHtml(rawMessage: string): string {
  const urlMatch = rawMessage.match(/https?:\/\/[^\s"'<>)]+/);
  const priceMatch = rawMessage.match(/(?:₹|Rs\.?|INR|\$)\s?[\d,]+(?:\.\d+)?/i);
  const directUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  const price = priceMatch ? priceMatch[0] : "";

  let showcaseHtml = "";
  if (directUrl || price) {
    showcaseHtml = `
      <div class="product-showcase">
        <h4 class="product-showcase__title">Mission Summary</h4>
        <div class="product-showcase__meta">
          ${price ? `<span class="price-tag">${escapeHtml(price)}</span>` : ""}
          ${directUrl ? `<a href="${escapeHtml(directUrl)}" target="_blank" rel="noopener noreferrer" class="product-link-btn">Open ${escapeHtml(linkLabel(directUrl))} ↗</a>` : ""}
        </div>
      </div>
    `;
  }

  return `
    ${showcaseHtml}
    <div class="result-raw-text">${linkify(rawMessage)}</div>
  `;
}

function addStepItem(message: string): void {
  if (!stepsList) return;
  // Clear placeholder if present
  const placeholder = stepsList.querySelector(".agent-step-placeholder");
  if (placeholder) placeholder.remove();

  stepCounter++;
  if (stepsCount) stepsCount.textContent = `${stepCounter} step${stepCounter === 1 ? "" : "s"} executed`;
  if (telStepsDone) telStepsDone.textContent = `${stepCounter} / 25`;

  const li = document.createElement("li");
  li.className = "agent-step-item";
  li.innerHTML = `
    <span class="agent-step-item__icon">${actionIcon(message)}</span>
    <span class="agent-step-item__text">${linkify(message)}</span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });

  // Update inspector URL if evidence contains a safe http(s) URL
  const urlMatch = message.match(/https?:\/\/[^\s"'<>)]+/);
  const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  if (safeUrl && inspectorUrl) {
    inspectorUrl.href = safeUrl;
    inspectorUrl.textContent = safeUrl;
  }
  if (inspectorEvidence) {
    inspectorEvidence.textContent = message;
  }
}

const START_MISSION_HTML = `
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
  Start Mission
`.trim();

const WORKING_MISSION_HTML = `
  <span class="btn-working-content">
    <svg class="btn-working-spinner" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
    Working<span class="btn-working-dots">...</span>
  </span>
`.trim();

function setButtonWorking(working: boolean): void {
  if (!btnStartMission) return;
  if (working) {
    btnStartMission.disabled = true;
    btnStartMission.classList.add("btn--working");
    btnStartMission.innerHTML = WORKING_MISSION_HTML;
  } else {
    btnStartMission.classList.remove("btn--working");
    btnStartMission.innerHTML = START_MISSION_HTML;
    btnStartMission.disabled = !missionInput?.value.trim();
  }
}

function renderCompletion(taskState: string, message: string): void {
  setTaskState(taskState);
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;
  if (humanGate) humanGate.hidden = true;

  if (resultCard && resultContent && resultBadge) {
    resultCard.hidden = false;
    resultTimestamp && (resultTimestamp.textContent = new Date().toLocaleTimeString());

    if (taskState === "completed") {
      resultCard.classList.remove("is-failed");
      resultBadge.textContent = "✓ Mission Completed";
      resultContent.innerHTML = formatResultHtml(message || "The autonomous task completed successfully.");
    } else {
      resultCard.classList.add("is-failed");
      resultBadge.textContent = "✗ Task Blocked / Incomplete";
      resultContent.innerHTML = `<div class="result-raw-text">${linkify(message || "Task stopped before completing all steps.")}</div>`;
    }
    resultCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function showHumanGate(msg: string, action?: unknown): void {
  if (!humanGate) return;
  if (humanGateMsg) humanGateMsg.textContent = msg || "Sign-in or authentication required to continue.";
  humanGate.hidden = false;
  humanGate.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideHumanGate(): void {
  if (humanGate) humanGate.hidden = true;
}

// Button Events
missionInput?.addEventListener("input", () => {
  if (btnStartMission) btnStartMission.disabled = !missionInput.value.trim();
});

// Preset Chips
document.querySelectorAll(".preset-chip").forEach(chip => {
  chip.addEventListener("click", () => {
    const prompt = chip.getAttribute("data-prompt");
    if (prompt && missionInput) {
      missionInput.value = prompt;
      if (btnStartMission) btnStartMission.disabled = false;
      missionInput.focus();
    }
  });
});

btnStartMission?.addEventListener("click", async () => {
  if (!missionInput || !btnStartMission || !missionInput.value.trim()) return;
  setButtonWorking(true);
  if (btnCancelMission) btnCancelMission.hidden = false;
  if (stepsList) stepsList.innerHTML = "";
  if (resultCard) resultCard.hidden = true;
  hideHumanGate();
  currentTaskId = null;
  cancelRequested = false;
  stepCounter = 0;
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  if (telStepsDone) telStepsDone.textContent = "0 / 25";
  if (telPlannerLatency) telPlannerLatency.textContent = "-";
  setTaskState("planning");

  try {
    const res = await fetch(`${API}/api/task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction: missionInput.value.trim() }),
    });
    if (!res.ok) throw new Error(`Backend response HTTP ${res.status}`);
    const data = (await res.json()) as { taskId: string };
    currentTaskId = data.taskId;

    // Cancel clicked while the task POST was in flight: honour it now that
    // the real task id exists instead of silently losing the request.
    if (cancelRequested) {
      try {
        await fetch(`${API}/api/task/${currentTaskId}/cancel`, { method: "POST" });
      } catch { /* offline */ }
      setTaskState("cancelled");
      if (btnCancelMission) btnCancelMission.hidden = true;
      setButtonWorking(false);
      return;
    }

    // Fetch initial task state
    const stRes = await fetch(`${API}/api/task/${currentTaskId}`).catch(() => null);
    if (stRes && stRes.ok) {
      const t = (await stRes.json()) as { taskState: string; message: string; screenshot?: string | null };
      if (t.taskState && t.taskState !== "planning") {
        setTaskState(t.taskState);
      }
      if (t.screenshot) {
        updateVisualFrame(t.screenshot, t.message || "", t.taskState);
      }
    }
  } catch (err) {
    setTaskState("failed");
    addStepItem(`⚠️ Backend connection failed: ${err}`);
    setButtonWorking(false);
    cancelRequested = false;
    if (btnCancelMission) btnCancelMission.hidden = true;
  }
});

btnCancelMission?.addEventListener("click", async () => {
  const id = currentTaskId;
  cancelRequested = true;
  if (btnCancelMission) btnCancelMission.hidden = true;
  // Close the human gate immediately: a cancelled task can never be approved.
  hideHumanGate();
  if (id) {
    try {
      await fetch(`${API}/api/task/${id}/cancel`, { method: "POST" });
    } catch {
      // Offline: the UI still reflects the user's intent.
    }
  }
  setTaskState("cancelled");
  setButtonWorking(false);
});

btnNewSession?.addEventListener("click", () => {
  currentTaskId = null;
  if (missionInput) missionInput.value = "";
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;
  if (stepsList) stepsList.innerHTML = '<li class="agent-step-placeholder">Submit a mission directive to begin autonomous browser execution.</li>';
  if (resultCard) resultCard.hidden = true;
  if (inspectorScreenshot) {
    inspectorScreenshot.hidden = true;
    inspectorScreenshot.removeAttribute("src");
  }
  if (inspectorScreenshotPlaceholder) inspectorScreenshotPlaceholder.hidden = false;
  if (inspectorUrl) {
    inspectorUrl.removeAttribute("href");
    inspectorUrl.textContent = "Waiting for task...";
  }
  if (inspectorTitle) inspectorTitle.textContent = "—";
  if (inspectorEvidence) inspectorEvidence.textContent = "Ready";
  if (obstacleBadge) {
    obstacleBadge.className = "obstacle-badge obstacle-badge--clear";
    obstacleBadge.textContent = "✓ Workflow Clear";
  }
  hideHumanGate();
  stepCounter = 0;
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  if (telStepsDone) telStepsDone.textContent = "0 / 25";
  setTaskState("ready");
});

btnGateApprove?.addEventListener("click", async () => {
  if (!currentTaskId) return;
  hideHumanGate();
  await fetch(`${API}/api/task/${currentTaskId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "approve" }),
  });
});

btnGateReject?.addEventListener("click", async () => {
  if (!currentTaskId) return;
  hideHumanGate();
  await fetch(`${API}/api/task/${currentTaskId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "reject" }),
  });
});

// WebSocket Client
function connectWebSocket(): void {
  const ws = new WebSocket(`ws://localhost:8000/ws/client`);

  ws.onopen = () => {
    setConnectionStatus(true, "Online");
  };

  ws.onmessage = ev => {
    try {
      const env = JSON.parse(ev.data) as {
        type: string;
        taskId: string;
        payload: {
          taskState: string;
          stepsDone: number;
          message: string;
          screenshot?: string | null;
          plannerMs?: number | null;
          observeMs?: number | null;
          actMs?: number | null;
        };
      };
      if (env.type !== "PROGRESS" || env.taskId !== currentTaskId) return;
      const p = env.payload;

      setTaskState(p.taskState);
      addStepItem(`[${p.stepsDone}] ${p.message}`);
      updateVisualFrame(p.screenshot, p.message, p.taskState);
      if (telPlannerLatency && typeof p.plannerMs === "number") {
        telPlannerLatency.textContent = `${(p.plannerMs / 1000).toFixed(1)}s`;
      }

      if (p.taskState === "awaiting_confirmation") {
        fetch(`${API}/api/task/${env.taskId}`)
          .then(r => r.json())
          .then(t => {
            const msg = t.pending_confirmation?.message ?? "Authentication / Sign-in required";
            showHumanGate(msg, t.pending_confirmation?.action);
          });
      }

      if (["completed", "failed", "cancelled"].includes(p.taskState)) {
        renderCompletion(p.taskState, p.message);
      }
    } catch {
      // non-JSON frame
    }
  };

  ws.onclose = () => {
    setConnectionStatus(false, "Reconnecting...");
    setTimeout(connectWebSocket, 2500);
  };

  ws.onerror = () => {
    setConnectionStatus(false, "Offline");
  };
}

connectWebSocket();
