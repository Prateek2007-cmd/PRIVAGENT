import { describe, expect, it } from "vitest";
import { Relay, type InboundMessage, type RelayMessage } from "../src/relay";

type Handler = (ev: { data: string }) => void;

class FakeSocket {
  sent: string[] = [];
  handlers: Handler[] = [];
  open = true;
  send(data: string) {
    this.sent.push(data);
  }
  addEventListener(_t: "message", cb: Handler) {
    this.handlers.push(cb);
  }
  emit(data: string) {
    for (const h of this.handlers) h({ data });
  }
}

describe("Relay", () => {
  it("sends a well-formed envelope", () => {
    const sock = new FakeSocket();
    const relay = new Relay();
    relay.attach(sock as unknown as WebSocket);
    relay.send({ type: "OBSERVATION", taskId: "t1", payload: { a: 1 } });
    expect(sock.sent).toEqual([
      JSON.stringify({ type: "OBSERVATION", taskId: "t1", payload: { a: 1 } }),
    ]);
  });

  it("dispatches inbound messages to handlers", () => {
    const sock = new FakeSocket();
    const relay = new Relay();
    const got: InboundMessage[] = [];
    relay.onMessage(m => got.push(m));
    relay.attach(sock as unknown as WebSocket);
    sock.emit(JSON.stringify({ type: "EXECUTE_ACTION", taskId: "t1", payload: {} }));
    expect(got).toEqual([{ type: "EXECUTE_ACTION", taskId: "t1", payload: {} }]);
  });

  it("drops unknown inbound message types silently", () => {
    const sock = new FakeSocket();
    const relay = new Relay();
    const got: InboundMessage[] = [];
    relay.onMessage(m => got.push(m));
    relay.attach(sock as unknown as WebSocket);
    sock.emit(JSON.stringify({ type: "EVIL", taskId: "t1", payload: {} }));
    expect(got).toEqual([]);
  });

  it("sends no-op when unattached", () => {
    const relay = new Relay();
    expect(() => relay.send({ type: "PROGRESS", taskId: "x", payload: null })).not.toThrow();
  });
});
