"use client";

import { FirebaseError, getApps, initializeApp } from "firebase/app";
import {
  browserSessionPersistence,
  getAuth,
  initializeAuth,
  type Auth,
  type User,
} from "firebase/auth";

export type StaffIdentity = {
  user_id: string;
  role: "staff";
};

let browserAuth: Auth | undefined;

export function getStaffAuth(): Auth {
  if (typeof window === "undefined") {
    throw new Error("Sign-in requires a browser.");
  }

  if (browserAuth) return browserAuth;

  const config = {
    apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
    authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  };

  if (Object.values(config).some((value) => !value)) {
    throw new Error("Staff sign-in is not configured.");
  }

  const existing = getApps().find(
    (app) => app.name === "restaurant-staff",
  );

  browserAuth = existing
    ? getAuth(existing)
    : initializeAuth(initializeApp(config, "restaurant-staff"), {
        persistence: browserSessionPersistence,
      });

  return browserAuth;
}

export class StaffApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "StaffApiError";
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  signal?: AbortSignal;
  user?: User | null;
};

async function staffRequest<T>(
  path: string,
  {
    method = "GET",
    body,
    signal,
    user = getStaffAuth().currentUser,
  }: RequestOptions = {},
): Promise<T> {
  if (!user) {
    throw new StaffApiError(401, "Please sign in again.");
  }

  const apiUrl = process.env.NEXT_PUBLIC_API_URL;

  if (!apiUrl) {
    throw new StaffApiError(
      503,
      "The staff service is not configured.",
    );
  }

  const controller = new AbortController();

  let rejectCancellation: (error: Error) => void = () => {};

  const cancelled = new Promise<never>((_, reject) => {
    rejectCancellation = reject;
  });

  const abort = () => {
    // Aborting the browser request does not undo a server transaction.
    controller.abort();
    rejectCancellation(
      new StaffApiError(503, "The request did not finish."),
    );
  };

  signal?.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(abort, 10_000);

  async function send(): Promise<T> {
    controller.signal.throwIfAborted();

    // Ask Firebase for a current ID token for every request.
    const token = await user!.getIdToken();

    // Token retrieval may finish after our timeout; do not send then.
    controller.signal.throwIfAborted();

    const response = await fetch(
      `${apiUrl!.replace(/\/$/, "")}${path}`,
      {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          ...(body === undefined
            ? {}
            : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        signal: controller.signal,
      },
    );

    if (response.status === 401) {
      throw new StaffApiError(
        401,
        "Your session expired. Sign out and sign in again.",
      );
    }

    if (response.status === 403) {
      throw new StaffApiError(
        403,
        "This account does not have staff access.",
      );
    }

        if ([404, 409].includes(response.status)) {
      const data = await response.json().catch(() => null);
      const detail = data?.detail;

      // Support both a plain HTTPException message and our structured errors.
      const message =
        typeof detail === "string" ? detail : detail?.message;

      throw new StaffApiError(
        response.status,
        typeof message === "string"
          ? message
          : "The requested action is unavailable.",
      );
    }

    if (response.status === 422) {
      throw new StaffApiError(
        422,
        "Check the action and its reason, then try again.",
      );
    }

    if (!response.ok) {
      throw new StaffApiError(
        response.status,
        "Kitchen data is temporarily unavailable.",
      );
    }

    return response.json() as Promise<T>;
  }

  try {
    if (signal?.aborted) abort();

    // Bound token retrieval as well as HTTP work.
    // Never automatically retry a write from this helper.
    return await Promise.race([send(), cancelled]);
  } catch (error) {
    if (error instanceof StaffApiError) throw error;

    if (
      error instanceof FirebaseError &&
      [
        "auth/user-disabled",
        "auth/user-token-expired",
        "auth/invalid-user-token",
      ].includes(error.code)
    ) {
      throw new StaffApiError(
        401,
        "Your session expired. Sign out and sign in again.",
      );
    }

    throw new StaffApiError(
      503,
      "Cannot reach the kitchen service. Check your connection.",
    );
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export function staffGet<T>(
  path: string,
  signal?: AbortSignal,
  user?: User | null,
) {
  return staffRequest<T>(path, { signal, user });
}

export function staffWrite<T>(
  path: string,
  method: "POST" | "PATCH",
  body?: unknown,
  signal?: AbortSignal,
) {
  // Allow an outstanding heartbeat to be cancelled when the page is hidden.
  return staffRequest<T>(path, { method, body, signal });
}

export function getStaffIdentity(
  user: User,
): Promise<StaffIdentity> {
  return staffGet<StaffIdentity>("/staff/me", undefined, user);
}