import { useCallback, useEffect, useRef, useState } from "react";
import {
  recordStatuses,
  summarizeTerminalTransitions,
  type RunLike,
} from "./notificationModel";

type NotificationPermissionState = "default" | "granted" | "denied" | "unsupported";

function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

function currentPermission(): NotificationPermissionState {
  if (!notificationsSupported()) return "unsupported";
  return Notification.permission as NotificationPermissionState;
}

export type UseTaskNotificationsResult = {
  /** Whether the browser exposes the Notification API at all. */
  supported: boolean;
  /** Current permission, kept in sync with the OS-level grant. */
  permission: NotificationPermissionState;
  /** Whether the user has flipped the "notify me" switch on. */
  enabled: boolean;
  /**
   * Toggle the switch. Turning it on for the first time triggers
   * `Notification.requestPermission()` — this MUST be called from a user
   * gesture (a real click) or the browser silently denies it. Returns the
   * resolved permission so the caller can surface a fallback toast.
   */
  toggle: (next: boolean) => Promise<NotificationPermissionState>;
};

const STORAGE_KEY = "cutagent_task_notifications_enabled_v1";

function readStoredEnabled(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * Watch a list of runs across polls and raise a single, merged browser
 * notification whenever runs reach a terminal state — regardless of whether the
 * tab is focused. Permission is only ever requested via {@link toggle} from a
 * user gesture. When notifications are unsupported or denied, `onFallback` is
 * invoked so the caller can keep the in-page toast path.
 */
export function useTaskNotifications({
  runs,
  enabled: enabledProp,
  onFallback,
}: {
  runs: RunLike[];
  /** Optional controlled enable flag; defaults to the locally persisted switch. */
  enabled?: boolean;
  /** Called (with the merged summary text) when a notification can't be shown. */
  onFallback?: (payload: { title: string; body: string }) => void;
}): UseTaskNotificationsResult {
  const supported = notificationsSupported();
  const [permission, setPermission] = useState<NotificationPermissionState>(currentPermission);
  const [internalEnabled, setInternalEnabled] = useState<boolean>(readStoredEnabled);
  const enabled = enabledProp ?? internalEnabled;
  const previousStatuses = useRef<Map<string, string>>(new Map());
  const fallbackRef = useRef(onFallback);
  fallbackRef.current = onFallback;

  const toggle = useCallback(
    async (next: boolean): Promise<NotificationPermissionState> => {
      if (!supported) {
        setInternalEnabled(false);
        return "unsupported";
      }
      if (!next) {
        setInternalEnabled(false);
        try {
          window.localStorage.setItem(STORAGE_KEY, "0");
        } catch {
          /* ignore storage errors */
        }
        return permission;
      }
      let resolved = currentPermission();
      if (resolved === "default") {
        try {
          resolved = (await Notification.requestPermission()) as NotificationPermissionState;
        } catch {
          resolved = "denied";
        }
      }
      setPermission(resolved);
      const granted = resolved === "granted";
      setInternalEnabled(granted);
      try {
        window.localStorage.setItem(STORAGE_KEY, granted ? "1" : "0");
      } catch {
        /* ignore storage errors */
      }
      return resolved;
    },
    [permission, supported],
  );

  useEffect(() => {
    const previous = previousStatuses.current;
    const summary = summarizeTerminalTransitions(runs, previous);
    recordStatuses(runs, previous);

    if (!summary.notification) return;
    const canNotify = supported && enabled && permission === "granted";
    if (canNotify) {
      try {
        // Fires regardless of focus — that is the whole point of system
        // notifications vs in-page toasts.
        // eslint-disable-next-line no-new
        new Notification(summary.notification.title, { body: summary.notification.body });
        return;
      } catch {
        /* fall through to the toast fallback */
      }
    }
    fallbackRef.current?.(summary.notification);
  }, [runs, enabled, permission, supported]);

  return { supported, permission, enabled, toggle };
}
