import "./styles/agent.css";
import { escapeHtml, linkify, linkLabel, safeHttpUrl, safeImageSrc } from "./sanitize";

const API = "http://localhost:8000";

// Workspace & Sidebar Elements
const agentWorkspace = document.getElementById("agentWorkspace") as HTMLElement | null;
const btnToggleSidebarHeader = document.getElementById("btnToggleSidebarHeader") as HTMLButtonElement | null;
const btnCollapseSidebar = document.getElementById("btnCollapseSidebar") as HTMLButtonElement | null;
const btnNewMissionSidebar = document.getElementById("btnNewMissionSidebar") as HTMLButtonElement | null;
const chatHistoryList = document.getElementById("chatHistoryList") as HTMLUListElement | null;

// Directive & Timeline Elements
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

// Session & Task State
let activeChatId: string | null = null;
let currentTaskId: string | null = null;
let stepCounter = 0;
let cancelRequested = false;

interface ChatSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  status: string;
}

interface ChatMessage {
  id: string;
  chat_id: string;
  role: "user" | "agent" | "system";
  content: string;
  evidence?: string | null;
  created_at: string;
}

interface ChatDetail {
  chat: ChatSummary;
  messages: ChatMessage[];
  memory: Record<string, string>;
}

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
  const hasObstacle = /\b(skip\s*ad|advertisement|cookie\s*banner|close\s*popup|modal\s*overlay|dismiss\s*dialog)\b/.test(m);

  if (obstacleBadge) {
    if (taskState === "awaiting_confirmation" || m.includes("authentication required") || m.includes("credentials required")) {
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

function addUserMessageItem(message: string): void {
  if (!stepsList) return;
  const placeholder = stepsList.querySelector(".agent-step-placeholder");
  if (placeholder) placeholder.remove();

  const li = document.createElement("li");
  li.className = "agent-step-item agent-step-item--user";
  li.innerHTML = `
    <span class="agent-step-item__icon">💬</span>
    <span class="agent-step-item__text">${linkify(message)}</span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function updateStepsCount(count: number): void {
  stepCounter = Math.max(0, count);
  if (stepsCount) stepsCount.textContent = `${stepCounter} step${stepCounter === 1 ? "" : "s"} executed`;
  if (telStepsDone) telStepsDone.textContent = `${stepCounter} / 25`;
}

function addStepItem(message: string, stepNum?: number): void {
  if (!stepsList) return;
  const placeholder = stepsList.querySelector(".agent-step-placeholder");
  if (placeholder) placeholder.remove();

  if (typeof stepNum === "number" && stepNum > 0) {
    updateStepsCount(stepNum);
  } else {
    updateStepsCount(stepCounter + 1);
  }

  // Normalize display: avoid double prefix like "[1] Step 1:"
  let displayMessage = message.trim();
  const stepPrefixRegex = /^\[\d+\]\s*/;
  if (stepPrefixRegex.test(displayMessage)) {
    displayMessage = displayMessage.replace(stepPrefixRegex, "");
  }

  const li = document.createElement("li");
  li.className = "agent-step-item";
  li.innerHTML = `
    <span class="agent-step-item__icon">${actionIcon(displayMessage)}</span>
    <span class="agent-step-item__text">${linkify(displayMessage)}</span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const urlMatch = displayMessage.match(/https?:\/\/[^\s"'<>)]+/);
  const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
  if (safeUrl && inspectorUrl) {
    inspectorUrl.href = safeUrl;
    inspectorUrl.textContent = safeUrl;
  }
  if (inspectorEvidence) {
    inspectorEvidence.textContent = displayMessage;
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

function addCompletionStepItem(taskState: string, message: string): void {
  if (!stepsList) return;
  const isOk = taskState === "completed";
  const li = document.createElement("li");
  li.className = isOk ? "agent-step-item agent-step-item--result" : "agent-step-item agent-step-item--failed";
  li.innerHTML = `
    <span class="agent-step-item__icon">${isOk ? "✓" : "✗"}</span>
    <span class="agent-step-item__text">
      <strong>${isOk ? "Mission Result" : "Task Incomplete"}</strong>:
      ${linkify(message || (isOk ? "Mission completed." : "Task stopped."))}
    </span>
  `;
  stepsList.appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderCompletion(taskState: string, message: string, appendToTimeline = true): void {
  setTaskState(taskState);
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;
  if (humanGate) humanGate.hidden = true;

  if (appendToTimeline) {
    addCompletionStepItem(taskState, message);
  }

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

function showHumanGate(msg: string): void {
  if (!humanGate) return;
  if (humanGateMsg) humanGateMsg.textContent = msg || "Sign-in or authentication required to continue.";
  humanGate.hidden = false;
  humanGate.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideHumanGate(): void {
  if (humanGate) humanGate.hidden = true;
}

// ----------------------------------------------------
// Multi-Turn Chat & Session Isolation Management
// ----------------------------------------------------

async function loadChatList(): Promise<void> {
  if (!chatHistoryList) return;
  try {
    const res = await fetch(`${API}/api/chats`);
    if (!res.ok) return;
    const chats = (await res.json()) as ChatSummary[];

    if (!Array.isArray(chats) || chats.length === 0) {
      chatHistoryList.innerHTML = `<li class="chat-history-empty">No saved missions yet</li>`;
      return;
    }

    chatHistoryList.innerHTML = "";
    chats.forEach(chat => {
      const li = document.createElement("li");
      li.className = `chat-history-item${chat.id === activeChatId ? " is-active" : ""}`;
      li.setAttribute("data-chat-id", chat.id);

      const dateObj = new Date(chat.updated_at || chat.created_at);
      const dateLabel = isNaN(dateObj.getTime())
        ? "Recent"
        : dateObj.toLocaleDateString(undefined, { month: "short", day: "numeric" });

      li.innerHTML = `
        <div class="chat-history-item__main">
          <span class="chat-history-item__title" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title || "Untitled Mission")}</span>
          <span class="chat-history-item__meta">${escapeHtml(dateLabel)}</span>
        </div>
        <button class="chat-delete-btn" title="Delete mission" aria-label="Delete mission">×</button>
      `;

      li.addEventListener("click", () => {
        if (activeChatId !== chat.id) {
          selectChat(chat.id);
        }
      });

      const delBtn = li.querySelector(".chat-delete-btn") as HTMLButtonElement | null;
      delBtn?.addEventListener("click", ev => {
        ev.stopPropagation();
        deleteChat(chat.id);
      });

      chatHistoryList.appendChild(li);
    });
  } catch {
    // Backend may still be spinning up
  }
}

async function selectChat(chatId: string): Promise<void> {
  activeChatId = chatId;
  currentTaskId = null;
  stepCounter = 0;
  hideHumanGate();
  if (btnCancelMission) btnCancelMission.hidden = true;
  setButtonWorking(false);

  // Update active state in sidebar list
  if (chatHistoryList) {
    chatHistoryList.querySelectorAll(".chat-history-item").forEach(el => {
      const elId = el.getAttribute("data-chat-id");
      if (elId === chatId) {
        el.classList.add("is-active");
      } else {
        el.classList.remove("is-active");
      }
    });
  }

  // Clear feed and load history
  if (stepsList) stepsList.innerHTML = "";
  if (resultCard) resultCard.hidden = true;
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  if (telStepsDone) telStepsDone.textContent = "0 / 25";
  setTaskState("ready");

  if (missionInput) {
    missionInput.value = "";
    missionInput.placeholder = "Ask a follow-up directive for this mission...";
    if (btnStartMission) btnStartMission.disabled = true;
  }

  try {
    const res = await fetch(`${API}/api/chat/${chatId}`);
    if (!res.ok) return;
    const detail = (await res.json()) as ChatDetail;

    if (!detail.messages || detail.messages.length === 0) {
      if (stepsList) {
        stepsList.innerHTML = '<li class="agent-step-placeholder">No messages in this mission thread yet.</li>';
      }
      return;
    }

    let lastAgentMsg: string | null = null;
    detail.messages.forEach(msg => {
      if (msg.role === "user") {
        addUserMessageItem(msg.content);
      } else if (msg.role === "agent") {
        lastAgentMsg = msg.content;
        addCompletionStepItem("completed", msg.content);
        if (msg.evidence) {
          const urlMatch = msg.evidence.match(/https?:\/\/[^\s"'<>)]+/);
          const safeUrl = urlMatch ? safeHttpUrl(urlMatch[0]) : null;
          if (safeUrl && inspectorUrl) {
            inspectorUrl.href = safeUrl;
            inspectorUrl.textContent = safeUrl;
          }
          if (inspectorEvidence) inspectorEvidence.textContent = msg.evidence;
        }
      }
    });

    if (lastAgentMsg) {
      renderCompletion("completed", lastAgentMsg, false);
    }
  } catch (err) {
    addStepItem(`⚠️ Failed to load chat history: ${err}`);
  }
}

async function deleteChat(chatId: string): Promise<void> {
  try {
    await fetch(`${API}/api/chat/${chatId}`, { method: "DELETE" });
  } catch {
    // Offline
  }

  if (activeChatId === chatId) {
    createNewChat();
  }
  await loadChatList();
}

function createNewChat(): void {
  activeChatId = null;
  currentTaskId = null;
  cancelRequested = false;
  stepCounter = 0;
  hideHumanGate();

  if (missionInput) {
    missionInput.value = "";
    missionInput.placeholder = "e.g. Buy me a yellow dress from amazon (proceed until checkout)";
  }
  setButtonWorking(false);
  if (btnCancelMission) btnCancelMission.hidden = true;

  if (stepsList) {
    stepsList.innerHTML = '<li class="agent-step-placeholder">Submit a mission directive to begin autonomous browser execution.</li>';
  }
  if (resultCard) resultCard.hidden = true;
  if (stepsCount) stepsCount.textContent = "0 steps executed";
  if (telStepsDone) telStepsDone.textContent = "0 / 25";
  if (telPlannerLatency) telPlannerLatency.textContent = "—";

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

  if (chatHistoryList) {
    chatHistoryList.querySelectorAll(".chat-history-item").forEach(el => el.classList.remove("is-active"));
  }

  setTaskState("ready");
}

// ----------------------------------------------------
// UI Events & Directive Execution
// ----------------------------------------------------

btnToggleSidebarHeader?.addEventListener("click", () => {
  agentWorkspace?.classList.toggle("sidebar-collapsed");
});

btnCollapseSidebar?.addEventListener("click", () => {
  agentWorkspace?.classList.add("sidebar-collapsed");
});

btnNewMissionSidebar?.addEventListener("click", () => {
  createNewChat();
});

btnNewSession?.addEventListener("click", () => {
  createNewChat();
});

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
  const instruction = missionInput.value.trim();

  setButtonWorking(true);
  if (btnCancelMission) btnCancelMission.hidden = false;
  if (resultCard) resultCard.hidden = true;
  hideHumanGate();
  currentTaskId = null;
  cancelRequested = false;

  // Append user instruction bubble immediately
  addUserMessageItem(instruction);
  missionInput.value = "";
  if (btnStartMission) btnStartMission.disabled = true;

  if (telPlannerLatency) telPlannerLatency.textContent = "—";
  setTaskState("planning");

  try {
    let res: Response;
    if (activeChatId) {
      res = await fetch(`${API}/api/chat/${activeChatId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      });
    } else {
      res = await fetch(`${API}/api/task`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      });
    }

    if (!res.ok) throw new Error(`Backend response HTTP ${res.status}`);
    const data = (await res.json()) as { task_id?: string; taskId?: string; chat_id?: string; chatId?: string };
    currentTaskId = data.task_id || data.taskId || null;
    activeChatId = activeChatId || data.chat_id || data.chatId || null;

    // Refresh chat list to show active title and timestamp
    loadChatList();

    // Cancel clicked while POST was in flight
    if (cancelRequested && currentTaskId) {
      try {
        await fetch(`${API}/api/task/${currentTaskId}/cancel`, { method: "POST" });
      } catch { /* offline */ }
      setTaskState("cancelled");
      if (btnCancelMission) btnCancelMission.hidden = true;
      setButtonWorking(false);
      return;
    }

    // Fetch initial task state if available
    if (currentTaskId) {
      const stRes = await fetch(`${API}/api/task/${currentTaskId}`).catch(() => null);
      if (stRes && stRes.ok) {
        const t = (await stRes.json()) as { taskState: string; message: string; screenshot?: string | null };
        if (t.taskState && t.taskState !== "planning") {
          setTaskState(t.taskState);
        }
        if (t.screenshot) {
          updateVisualFrame(t.screenshot, t.message || "", t.taskState);
        }
        if (["completed", "failed", "cancelled"].includes(t.taskState)) {
          renderCompletion(t.taskState, t.message || "");
          loadChatList();
        }
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
  hideHumanGate();
  if (id) {
    try {
      await fetch(`${API}/api/task/${id}/cancel`, { method: "POST" });
    } catch {
      // Offline
    }
  }
  setTaskState("cancelled");
  setButtonWorking(false);
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

// ----------------------------------------------------
// WebSocket Client
// ----------------------------------------------------

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
          chatId?: string | null;
          taskState: string;
          stepsDone: number;
          message: string;
          screenshot?: string | null;
          plannerMs?: number | null;
          observeMs?: number | null;
          actMs?: number | null;
        };
      };
      if (env.type !== "PROGRESS") return;
      if (env.taskId !== currentTaskId && env.payload?.chatId !== activeChatId) return;
      const p = env.payload;

      setTaskState(p.taskState);
      addStepItem(p.message, p.stepsDone);
      updateVisualFrame(p.screenshot, p.message, p.taskState);
      if (telPlannerLatency && typeof p.plannerMs === "number") {
        telPlannerLatency.textContent = `${(p.plannerMs / 1000).toFixed(1)}s`;
      }

      if (p.taskState === "awaiting_confirmation") {
        fetch(`${API}/api/task/${env.taskId}`)
          .then(r => r.json())
          .then(t => {
            const msg = t.pending_confirmation?.message ?? "Authentication / Sign-in required";
            showHumanGate(msg);
          });
      }

      if (["completed", "failed", "cancelled"].includes(p.taskState)) {
        renderCompletion(p.taskState, p.message);
        loadChatList();
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

// Initial Load
connectWebSocket();
loadChatList();
