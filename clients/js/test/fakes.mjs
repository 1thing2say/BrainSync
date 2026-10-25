// Test doubles for the client SDK: a fake WebSocket class and fake timers. No network.
export function makeFakeWS() {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) {
      if (FakeWebSocket.throwNext) {
        FakeWebSocket.throwNext = false;
        throw new SyntaxError("bad url");
      }
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.closedWith = null;
      sockets.push(this);
    }
    send(text) {
      if (this.readyState !== 1) throw new Error("not open");
      this.sent.push(JSON.parse(text));
    }
    close(code, reason) { this.readyState = 3; this.closedWith = { code, reason }; }
    // driven by the tests:
    accept() { this.readyState = 1; this.onopen && this.onopen({}); }
    message(obj) { this.onmessage({ data: typeof obj === "string" ? obj : JSON.stringify(obj) }); }
    drop(code = 1006, reason = "") { this.readyState = 3; this.onclose && this.onclose({ code, reason }); }
    types() { return this.sent.map((m) => m.type); }
  }
  FakeWebSocket.throwNext = false;
  return { FakeWebSocket, sockets };
}

export function makeFakeTimers() {
  let now = 1_000_000;
  let nextId = 1;
  const pending = new Map();   // id -> {at, f, every}
  const timers = {
    setTimeout(f, ms) { const id = nextId++; pending.set(id, { at: now + ms, f, every: null, ms }); return id; },
    clearTimeout(id) { pending.delete(id); },
    setInterval(f, ms) { const id = nextId++; pending.set(id, { at: now + ms, f, every: ms, ms }); return id; },
    clearInterval(id) { pending.delete(id); },
  };
  function advance(ms) {
    const end = now + ms;
    for (;;) {
      let best = null;
      for (const [id, t] of pending) if (t.at <= end && (best === null || t.at < best[1].at)) best = [id, t];
      if (best === null) break;
      const [id, t] = best;
      now = t.at;
      if (t.every === null) pending.delete(id); else t.at += t.every;
      t.f();
    }
    now = end;
  }
  const timeouts = () => [...pending.values()].filter((t) => t.every === null).map((t) => t.ms);
  return { timers, advance, now: () => now, pending, timeouts };
}
