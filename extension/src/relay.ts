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
      if (!m.type || !INBOUND_TYPES.has(m.type) || typeof m.taskId !== "string") return;
      for (const h of this.handlers) {
        h({ type: m.type as "EXECUTE_ACTION", taskId: m.taskId, payload: m.payload ?? {} });
      }
    });
  }

  onMessage(cb: (m: InboundMessage) => void): void {
    this.handlers.push(cb);
  }

  send(msg: RelayMessage): void {
    this.socket?.send(JSON.stringify(msg));
  }
}
