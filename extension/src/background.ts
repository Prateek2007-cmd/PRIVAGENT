import { Relay } from "./relay";
import { executeAction } from "./actionExecutor";

const BACKEND_WS = "ws://localhost:8000/ws/extension";
const RECONNECT_MS = 2000;

const relay = new Relay();
let socket: WebSocket | null = null;
let isConnecting = false;

function connect() {
  if (isConnecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  isConnecting = true;
  try {
    const ws = new WebSocket(BACKEND_WS);
    socket = ws;
    ws.addEventListener("open", () => {
      console.log("[navigator] relay connected to backend");
      isConnecting = false;
    });
    ws.addEventListener("close", () => {
      socket = null;
      isConnecting = false;
      setTimeout(connect, RECONNECT_MS);
    });
    ws.addEventListener("error", () => {
      try { ws.close(); } catch { /* ignore */ }
    });
    relay.attach(ws);
  } catch {
    socket = null;
    isConnecting = false;
    setTimeout(connect, RECONNECT_MS);
  }
}

relay.onMessage(async m => {
  try {
    const action = m.payload as Parameters<typeof executeAction>[0];
    const result = await executeAction(action, m.taskId);
    relay.send({ type: "RESULT", taskId: m.taskId, payload: result });
  } catch (e) {
    relay.send({
      type: "RESULT",
      taskId: m.taskId,
      payload: { ok: false, error: { code: "UNKNOWN", message: String(e), recoverable: true } },
    });
  }
});

// Alarm heartbeat keeps service worker connected to the backend
chrome.alarms?.create("keepAlive", { periodInMinutes: 0.25 });
chrome.alarms?.onAlarm.addListener(alarm => {
  if (alarm.name === "keepAlive") {
    connect();
  }
});

connect();

