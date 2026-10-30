export type RelayMessage = {
  type: "OBSERVATION" | "RESULT" | "PROGRESS";
  taskId: string;
  payload: unknown;
};

export type InboundMessage = {
  type: "EXECUTE_ACTION";
  taskId: string;
  payload: unknown;
};

const INBOUND_TYPES = new Set(["EXECUTE_ACTION"]);

/** Frame around a WebSocket: outbound envelopes + inbound dispatch.
 * Routing logic is pure and testable without Chrome. */
export class Relay {
  private socket: WebSocket | null = null;
  private handlers: ((m: InboundMessage) => void)[] = [];

  attach(socket: WebSocket): void {
    this.socket = socket;
    socket.addEventListener("message", ev => {
      let msg: unknown;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (typeof msg !== "object" || msg === null) return;
      const m = msg as { type?: string; taskId?: string; payload?: unknown };
      if (!m.type || !INBOUND_TYPES.has(m.type) || typeof m.taskId !== "string" || !m.taskId) return;
      for (const h of this.handlers) {
        try {
          h({ type: m.type as "EXECUTE_ACTION", taskId: m.taskId, payload: m.payload ?? {} });
        } catch {
          // A handler bug must not tear down the socket listener.
        }
      }
    });
  }

  /** Detach a socket that is closing so later sends cannot target it. */
  detach(socket: WebSocket): void {
    if (this.socket === socket) this.socket = null;
  }

  onMessage(cb: (m: InboundMessage) => void): void {
    this.handlers.push(cb);
  }

  /** Returns false (instead of throwing) when the relay is offline. */
  send(msg: RelayMessage): boolean {
    const ws = this.socket as (WebSocket & { readyState?: number }) | null;
    if (!ws) return false;
    const open = typeof WebSocket !== "undefined" ? WebSocket.OPEN : 1;
    // Sockets without readyState (test doubles) are treated as open.
    if (ws.readyState !== undefined && ws.readyState !== open) return false;
    try {
      ws.send(JSON.stringify(msg));
      return true;
    } catch {
      return false;
    }
  }
}
