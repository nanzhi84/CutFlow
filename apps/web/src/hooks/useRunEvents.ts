import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

export type RunEventMessage = {
  event_id?: string;
  run_id?: string;
  job_id?: string;
  event_type?: "run_update" | "node_update" | "artifact_created" | "warning" | "error" | "heartbeat";
  server_time?: string;
  node_id?: string | null;
  status?: string | null;
  progress?: number | null;
  message?: string;
  created_at?: string;
};

export type RunEventState = "idle" | "connecting" | "live" | "reconnecting" | "closed" | "error";

function streamUrl(path: string, token: string) {
  const base = window.location.origin.replace(/^http/, "ws");
  const url = new URL(path, base);
  url.searchParams.set("token", token);
  return url.toString();
}

function parseEvent(raw: MessageEvent<string>): RunEventMessage | null {
  try {
    const payload = JSON.parse(raw.data) as RunEventMessage;
    return typeof payload === "object" && payload !== null ? payload : null;
  } catch {
    return null;
  }
}

export function useRunEvents(runId: string | null | undefined, enabled = true) {
  const [events, setEvents] = useState<RunEventMessage[]>([]);
  const [state, setState] = useState<RunEventState>("idle");
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!runId || !enabled) {
      setState("idle");
      setEvents([]);
      seen.current.clear();
      return;
    }
    const activeRunId = runId;

    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let attempt = 0;

    function scheduleReconnect() {
      if (stopped) return;
      attempt += 1;
      reconnectTimer = window.setTimeout(connect, Math.min(15000, 800 * 2 ** Math.min(attempt, 8)));
    }

    function kickReconnect() {
      if (stopped || socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      void connect();
    }

    async function connect() {
      setState(attempt === 0 ? "connecting" : "reconnecting");
      try {
        const token = await api.runs.events(activeRunId);
        if (stopped) return;
        socket = new WebSocket(streamUrl(token.stream_url, token.token));
        socket.onopen = () => {
          attempt = 0;
          setState("live");
        };
        socket.onmessage = (message) => {
          const event = parseEvent(message);
          if (!event) return;
          // Server heartbeats only keep the proxy connection alive; they carry
          // no run state, so don't add them to the event list (issue #74).
          if (event.event_type === "heartbeat") return;
          const key = event.event_id ?? `${event.event_type}:${event.node_id}:${event.status}:${event.created_at}`;
          if (key && seen.current.has(key)) return;
          if (key) seen.current.add(key);
          setEvents((current) => [...current, event].slice(-80));
        };
        socket.onerror = () => {
          setState("error");
        };
        socket.onclose = () => {
          if (stopped) {
            setState("closed");
            return;
          }
          scheduleReconnect();
        };
      } catch {
        if (stopped) return;
        setState("error");
        scheduleReconnect();
      }
    }

    void connect();
    window.addEventListener("online", kickReconnect);
    document.addEventListener("visibilitychange", kickReconnect);
    return () => {
      stopped = true;
      window.removeEventListener("online", kickReconnect);
      document.removeEventListener("visibilitychange", kickReconnect);
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      socket?.close();
    };
  }, [enabled, runId]);

  return { events, state };
}
