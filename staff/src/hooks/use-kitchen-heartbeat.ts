"use client";

import { useCallback, useEffect, useRef } from "react";
import { StaffApiError, staffWrite } from "@/lib/auth";

export function useKitchenHeartbeat(
  enabled: boolean,
  onAuthError: (message: string) => void,
) {
  const active = useRef(false);
  const pending = useRef<AbortController | null>(null);

  useEffect(() => {
    active.current = enabled;

    const hide = () => {
      if (document.hidden) {
        pending.current?.abort();
      }
    };

    document.addEventListener("visibilitychange", hide);

    return () => {
      active.current = false;
      pending.current?.abort();
      document.removeEventListener("visibilitychange", hide);
    };
  }, [enabled]);

  return useCallback(() => {
    // Call this only after successfully receiving the order queue.
    // A separate timer could incorrectly report a broken dashboard as healthy.
    if (
      !enabled ||
      !active.current ||
      document.hidden ||
      pending.current
    ) {
      return;
    }

    const controller = new AbortController();
    pending.current = controller;

    void staffWrite<{ ok: boolean }>(
      "/staff/kitchen/heartbeat",
      "POST",
      undefined,
      controller.signal,
    )
      .catch((error: unknown) => {
        if (!active.current || controller.signal.aborted) return;

        if (
          error instanceof StaffApiError &&
          [401, 403].includes(error.status)
        ) {
          onAuthError(error.message);
        }

        // Other failures are retried after the next successful queue read.
        // Without a successful heartbeat, the database deadline expires.
      })
      .finally(() => {
        if (pending.current === controller) {
          pending.current = null;
        }
      });
  }, [enabled, onAuthError]);
}