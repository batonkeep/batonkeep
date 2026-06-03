// useLiveFeed.ts — a single, app-wide, auto-reconnecting WebSocket to /ws.
//
// One socket is shared across the whole app (singleton). Two hooks consume it:
//   • useLiveFeed()        → connection status + a map of the latest Run state by id
//   • useRunEvents(runId)  → the ordered event stream for one run, with token text
//                            accumulated locally so high-frequency tokens don't churn
//                            the rest of the app.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Run, RunEvent, WsMessage } from "./types";

export type WsStatus = "connecting" | "open" | "closed";

interface Snapshot {
  status: WsStatus;
  runs: Record<number, Run>;
}

type RawListener = (msg: WsMessage) => void;

class LiveFeed {
  private started = false;
  private reconnectDelay = 1000;
  private rawListeners = new Set<RawListener>();
  private changeListeners = new Set<() => void>();
  private snapshot: Snapshot = { status: "connecting", runs: {} };

  start() {
    if (this.started) return;
    this.started = true;
    this.connect();
  }

  private connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.setStatus("connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      this.reconnectDelay = 1000;
      this.setStatus("open");
    };
    ws.onmessage = (e) => {
      try {
        this.handle(JSON.parse(e.data) as WsMessage);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      this.setStatus("closed");
      this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect() {
    setTimeout(() => this.connect(), this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 15000);
  }

  private handle(msg: WsMessage) {
    if (msg.type === "run.update") {
      this.snapshot = {
        ...this.snapshot,
        runs: { ...this.snapshot.runs, [msg.run.id]: msg.run },
      };
      this.emitChange();
    }
    // Fan out every frame to raw listeners (per-run event subscribers).
    this.rawListeners.forEach((l) => l(msg));
  }

  private setStatus(status: WsStatus) {
    if (this.snapshot.status === status) return;
    this.snapshot = { ...this.snapshot, status };
    this.emitChange();
  }

  private emitChange() {
    this.changeListeners.forEach((l) => l());
  }

  // useSyncExternalStore wiring for the global snapshot.
  subscribe = (cb: () => void) => {
    this.changeListeners.add(cb);
    this.start();
    return () => this.changeListeners.delete(cb);
  };
  getSnapshot = () => this.snapshot;

  // Raw-frame subscription for per-run event streams.
  onMessage(cb: RawListener) {
    this.rawListeners.add(cb);
    this.start();
    return () => this.rawListeners.delete(cb);
  }
}

const feed = new LiveFeed();

/** Connection status + latest Run state for every run we've seen update live. */
export function useLiveFeed() {
  const snap = useSyncExternalStore(feed.subscribe, feed.getSnapshot, feed.getSnapshot);
  return { status: snap.status, liveRuns: snap.runs };
}

/**
 * Ordered event stream for a single run. `events` excludes token frames;
 * `streamingText` is the concatenation of token deltas (for the live report preview).
 */
export function useRunEvents(runId: number | null) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const seenSeqs = useRef<Set<number>>(new Set());

  useEffect(() => {
    setEvents([]);
    setStreamingText("");
    seenSeqs.current = new Set();
    if (runId == null) return;

    const off = feed.onMessage((msg) => {
      if (msg.type !== "run.event" || msg.run_id !== runId) return;
      const ev = msg.event;
      if (ev.kind === "token") {
        if (ev.text) setStreamingText((t) => t + ev.text);
        return;
      }
      // De-dupe by seq (events may also be backfilled via REST).
      if (seenSeqs.current.has(ev.seq)) return;
      seenSeqs.current.add(ev.seq);
      setEvents((prev) => [...prev, ev]);
    });
    return () => {
      off();
    };
  }, [runId]);

  // Allow callers to seed the stream with events fetched over REST (history).
  const seed = (initial: RunEvent[]) => {
    for (const ev of initial) seenSeqs.current.add(ev.seq);
    setEvents(initial.filter((e) => e.kind !== "token"));
  };

  return { events, streamingText, seedEvents: seed };
}
