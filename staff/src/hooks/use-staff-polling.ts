"use client";

import { useEffect, useState } from "react";
import { StaffApiError, staffGet } from "@/lib/auth";

type Snapshot<T> = {
  path: string | null;
  refreshKey: number;
  data: T | null;
  error: string;
  updatedAt: number | null;
};

export function useStaffPolling<T>(
  path: string | null,
  onAuthError: (message: string) => void,
  onData?: (data: T) => void,
  refreshKey = 0,
) {
  const [state, setState] = useState<Snapshot<T>>({
    path: null,
    refreshKey: -1,
    data: null,
    error: "",
    updatedAt: null,
  });

  useEffect(() => {
    if (!path) return;

    let active = true;
    let running = false;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;

    async function poll() {
      if (!active || running || stopped) return;

      running = true;
      clearTimeout(timer);
      controller = new AbortController();

      try {
        const data = await staffGet<T>(
          path!,
          controller.signal,
        );

        if (!active) return;

        onData?.(data);

        setState({
          path,
          refreshKey,
          data,
          error: "",
          updatedAt: Date.now(),
        });
      } catch (error) {
        if (!active) return;

        const message =
          error instanceof Error
            ? error.message
            : "Cannot load orders.";

        if (
          error instanceof StaffApiError &&
          [401, 403].includes(error.status)
        ) {
          stopped = true;

          // Remove protected data when staff access is lost.
          setState({
            path,
            refreshKey,
            data: null,
            error: message,
            updatedAt: null,
          });

          onAuthError(message);
        } else {
          const missing =
            error instanceof StaffApiError &&
            error.status === 404;

          stopped = missing;

          setState((previous) => ({
            path,
            refreshKey,
            data:
              !missing && previous.path === path
                ? previous.data
                : null,
            updatedAt:
              !missing && previous.path === path
                ? previous.updatedAt
                : null,
            error: message,
          }));
        }
      } finally {
        running = false;

        // Wait until this request finishes before scheduling another.
        if (active && !stopped) {
          timer = setTimeout(() => void poll(), 3_000);
        }
      }
    }

    const wake = () => {
      if (!document.hidden) void poll();
    };

    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    void poll();

    return () => {
      // A changed refreshKey invalidates responses from the previous effect.
      active = false;
      clearTimeout(timer);
      controller?.abort();

      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [path, refreshKey, onAuthError, onData]);

  if (!path || state.path !== path) {
    // Never show another order's details while a new selection loads.
    return {
      data: null,
      error: "",
      updatedAt: null,
      refreshing: Boolean(path),
    };
  }

  return {
    data: state.data,
    error: state.error,
    updatedAt: state.updatedAt,

    // Keep the last snapshot visible, but disable actions while refreshing.
    refreshing: state.refreshKey !== refreshKey,
  };
}