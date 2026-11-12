"use client";

import { FirebaseError,  getApps, initializeApp } from "firebase/app";
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
    throw new Error("Staff sign-in is only available in the browser.");
  }

  if (browserAuth) return browserAuth;

  const config = {
    apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
    authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  };

  if (Object.values(config).some((value) => !value)) {
    throw new Error("Staff sign-in configuration is missing.");
  }

  const existing = getApps().find(
    (app) => app.name === "restaurant-staff",
  );

  browserAuth = existing
    ? getAuth(existing)
    : initializeAuth(
        initializeApp(config, "restaurant-staff"),
        { persistence: browserSessionPersistence },
      );

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

export async function staffGet<T>(
  path: string,
  signal?: AbortSignal,
  user: User | null = getStaffAuth().currentUser,
): Promise<T> {
  if (!user) {
    throw new StaffApiError(401, "Please sign in again.");
  }

  const apiUrl = process.env.NEXT_PUBLIC_API_URL;

  if (!apiUrl) {
    throw new StaffApiError(
      503,
      "The staff API address is missing.",
    );
  }

  const controller = new AbortController();
  const abort = () => controller.abort();

  const cancelled = new Promise<never>((_, reject) => {
    controller.signal.addEventListener(
      "abort",
      () =>
        reject(
          new StaffApiError(
            503,
            "The request did not finish. Please try again.",
          ),
        ),
      { once: true },
    );
  });

  signal?.addEventListener("abort", abort, { once: true });

  if (signal?.aborted) {
    abort();
  }

  const timer = setTimeout(abort, 10_000);

  async function send(): Promise<T> {
    const token = await user!.getIdToken();

    controller.signal.throwIfAborted();

    const response = await fetch(
      `${apiUrl!.replace(/\/$/, "")}${path}`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
        },
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

    if (response.status === 404) {
      throw new StaffApiError(404, "Order not found.");
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
    return await Promise.race([send(), cancelled]);
  } catch (error) {
    if (error instanceof StaffApiError) {
      throw error;
    }

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
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export function getStaffIdentity(
  user: User,
): Promise<StaffIdentity> {
  return staffGet<StaffIdentity>(
    "/staff/me",
    undefined,
    user,
  );
}