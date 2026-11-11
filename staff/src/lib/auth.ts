"use client";

import { getApps, initializeApp } from "firebase/app";
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

export async function getStaffIdentity(
  user: User,
): Promise<StaffIdentity> {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL;
  if (!apiUrl) {
    throw new Error("The staff API address is missing.");
  }

  let response: Response;

  try {
    const token = await user.getIdToken();

    response = await fetch(
      `${apiUrl.replace(/\/$/, "")}/staff/me`,
      {
        headers: { Authorization: `Bearer ${token}` },
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(10_000),
      },
    );
  } catch {
    throw new Error(
      "Could not check staff access. Try again or sign out and sign in.",
    );
  }

  if (response.status === 401) {
    throw new Error(
      "Your staff session has expired. Sign out and sign in again.",
    );
  }

  if (response.status === 403) {
    throw new Error(
      "This account has not been approved for staff access.",
    );
  }

  if (!response.ok) {
    throw new Error(
      "Staff access could not be checked. Please try again.",
    );
  }

  return response.json() as Promise<StaffIdentity>;
}