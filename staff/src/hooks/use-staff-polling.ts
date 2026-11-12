"use client";

import { useEffect, useState } from "react";
import { StaffApiError, staffGet } from "@/lib/auth";

type Snapshot<T> = {
  path: string | null;
  data: T | null;
  error: string;
  updatedAt: number | null;
};

export function useStaffPolling<T>(
  path: string | null,
  onAuthError: (message: string) => void,
  onData?: (data: T) => void,
) {
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({
    path: null,
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

    const endpoint = path;

    async function poll() {
      if (!active || running || stopped) return;

      running = true;
      clearTimeout(timer);
      controller = new AbortController();

      try {
        const data = await staffGet<T>(
          endpoint,
          controller.signal,
        );

        if (!active) return;

        onData?.(data);

        setSnapshot({
          path: endpoint,
          data,
          error: "",
          updatedAt: Date.now(),
        });
      } catch (error) {
        if (!active) return;

        const message =
          error instanceof Error
            ? error.message
            : "Could not load orders.";

        if (
          error instanceof StaffApiError &&
          [401, 403].includes(error.status)
        ) {
          stopped = true;

          setSnapshot({
            path: endpoint,
            data: null,
            error: message,
            updatedAt: null,
          });

          onAuthError(message);
        } else {
          const missing =
            error instanceof StaffApiError &&
            error.status === 404;

          setSnapshot((previous) => ({
            path: endpoint,
            data:
              !missing && previous.path === endpoint
                ? previous.data
                : null,
            updatedAt:
              !missing && previous.path === endpoint
                ? previous.updatedAt
                : null,
            error: message,
          }));

          stopped = missing;
        }
      } finally {
        running = false;

        if (active && !stopped) {
          timer = setTimeout(poll, 3000);
        }
      }
    }

    const wake = () => {
      if (document.visibilityState === "visible") {
        void poll();
      }
    };

    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);

    void poll();

    return () => {
      active = false;
      clearTimeout(timer);
      controller?.abort();

      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [path, onAuthError, onData]);

  return path && snapshot.path === path
    ? snapshot
    : {
        path,
        data: null,
        error: "",
        updatedAt: null,
      };
}