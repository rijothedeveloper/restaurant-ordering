"use client";

import { useRef, useState } from "react";

import { useStaffPolling } from "@/hooks/use-staff-polling";
import { StaffApiError, staffWrite } from "@/lib/auth";
import { localTime } from "@/lib/orders";

type AvailableOption = {
  id: string;
  name: string;
  is_available: boolean;
};

type AvailableItem = AvailableOption & {
  modifier_groups: {
    id: string;
    name: string;
    options: AvailableOption[];
  }[];
};

type KitchenOverview = {
  ordering_enabled: boolean;
  kitchen_online: boolean;
  accepting_orders: boolean;
  block_reason: string | null;
  kitchen_last_seen_at: string | null;
  menu: {
    categories: {
      id: string;
      name: string;
      items: AvailableItem[];
    }[];
  };
};

const REASONS: Record<string, string> = {
  ordering_paused: "Ordering is paused by staff.",
  tax_not_configured: "The restaurant tax rate needs to be configured.",
  restaurant_closed: "Outside restaurant opening hours.",
  kitchen_offline: "Waiting for a connected kitchen screen.",
};

export function KitchenControls({
  now,
  disabled,
  onAuthError,
}: {
  now: number;
  disabled: boolean;
  onAuthError: (message: string) => void;
}) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const inFlight = useRef(false);

  const overview = useStaffPolling<KitchenOverview>(
    "/staff/kitchen",
    onAuthError,
    undefined,
    refreshKey,
  );

  const stale =
    overview.updatedAt === null ||
    Boolean(overview.error) ||
    now - overview.updatedAt > 10_000;

  const blocked =
    disabled || busy || stale || overview.refreshing;

  const data = overview.data;

  async function save(path: string, body: unknown) {
    // The ref prevents another click before React renders disabled controls.
    if (blocked || inFlight.current) return;

    inFlight.current = true;
    setBusy(true);
    setMessage("");

    try {
      await staffWrite<{ ok: boolean }>(path, "PATCH", body);
      setMessage("Saved.");
    } catch (error) {
      if (
        error instanceof StaffApiError &&
        [401, 403].includes(error.status)
      ) {
        onAuthError(error.message);
      } else {
        const rejected =
          error instanceof StaffApiError &&
          [404, 409, 422].includes(error.status);

        setMessage(
          rejected
            ? error.message
            : "The result could not be confirmed. Check the refreshed settings before trying again.",
        );
      }
    } finally {
      inFlight.current = false;
      setBusy(false);

      // Invalidate pre-write responses and read the saved values.
      // Do not automatically replay a command after a timeout.
      setRefreshKey((value) => value + 1);
    }
  }

  function availability(
    kind: "items" | "options",
    id: string,
    available: boolean,
  ) {
    void save(`/staff/menu/${kind}/${id}/availability`, {
      is_available: available,
    });
  }

  return (
    <section
      className="panel kitchen-controls"
      aria-labelledby="controls-title"
    >
      <h2 id="controls-title">Kitchen controls</h2>

      {message && <p role="status">{message}</p>}

      {!data && (
        <p>{overview.error || "Loading kitchen settings…"}</p>
      )}

      {data && (
        <>
          <p role="status">
            {stale || overview.refreshing
              ? "Checking the current intake status…"
              : data.accepting_orders
                ? "Accepting new orders."
                : REASONS[data.block_reason ?? ""] ??
                  "Ordering is unavailable."}
          </p>

          <p>
            {data.ordering_enabled
              ? "Ordering enabled by staff."
              : "Ordering paused by staff."}
            {" "}
            {data.kitchen_online
              ? "Kitchen connected."
              : "Kitchen not connected."}
          </p>

          {data.kitchen_last_seen_at && (
            <p className="muted">
              Last kitchen check-in:{" "}
              {localTime(data.kitchen_last_seen_at)}
            </p>
          )}

          {overview.error && (
            <p role="alert">{overview.error}</p>
          )}

          <fieldset disabled={blocked}>
            <legend className="muted">New order intake</legend>

            <button
              type="button"
              onClick={() =>
                void save("/staff/kitchen/intake", {
                  ordering_enabled: !data.ordering_enabled,
                })
              }
            >
              {data.ordering_enabled
                ? "Pause intake"
                : "Resume intake"}
            </button>

            <p className="muted">
              Resuming also requires opening hours and a connected
              kitchen screen. Existing orders remain in the queue
              while intake is paused.
            </p>

            <details>
              <summary>Menu availability</summary>

              <p className="muted">
                Checked means available. Saved orders keep their
                original items and prices.
              </p>

              {data.menu.categories.map((category) => (
                <section key={category.id}>
                  <h3>{category.name}</h3>

                  {category.items.map((item) => (
                    <div
                      className="availability-item"
                      key={item.id}
                    >
                      <label className="availability-row">
                        <input
                          type="checkbox"
                          checked={item.is_available}
                          onChange={(event) =>
                            availability(
                              "items",
                              item.id,
                              event.target.checked,
                            )
                          }
                        />
                        <strong>{item.name}</strong>
                      </label>

                      {item.modifier_groups.map((group) => (
                        <div
                          className="availability-options"
                          key={group.id}
                        >
                          <p className="muted">{group.name}</p>

                          {group.options.map((option) => (
                            <label
                              className="availability-row"
                              key={option.id}
                            >
                              <input
                                type="checkbox"
                                checked={option.is_available}
                                aria-label={`${item.name}, ${group.name}, ${option.name}`}
                                onChange={(event) =>
                                  availability(
                                    "options",
                                    option.id,
                                    event.target.checked,
                                  )
                                }
                              />
                              {option.name}
                            </label>
                          ))}
                        </div>
                      ))}
                    </div>
                  ))}
                </section>
              ))}

              {!data.menu.categories.some(
                (category) => category.items.length,
              ) && <p>No menu items configured.</p>}
            </details>
          </fieldset>

          {busy && <p role="status">Saving…</p>}
        </>
      )}
    </section>
  );
}