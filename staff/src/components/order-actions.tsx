"use client";

import { useEffect, useRef, useState } from "react";
import { StaffApiError, staffWrite } from "@/lib/auth";
import type { OrderDetails, OrderStatus } from "@/lib/orders";

type Props = {
  order: OrderDetails;
  disabled: boolean;
  onChanged: () => void;
  onAuthError: (message: string) => void;
};

const NEXT: Partial<
  Record<OrderStatus, { status: OrderStatus; label: string }>
> = {
  PLACED: {
    status: "ACCEPTED",
    label: "Accept order",
  },
  ACCEPTED: {
    status: "PREPARING",
    label: "Start preparing",
  },
  PREPARING: {
    status: "READY",
    label: "Mark ready",
  },
  READY: {
    status: "COMPLETED",
    label: "Complete order",
  },
};

export function OrderActions({
  order,
  disabled,
  onChanged,
  onAuthError,
}: Props) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const inFlight = useRef(false);
  const mounted = useRef(false);

  const next = NEXT[order.status];
  const unpaid = order.payment_status === "UNPAID";

  useEffect(() => {
    mounted.current = true;

    return () => {
      mounted.current = false;
    };
  }, []);

  async function run(target: OrderStatus | "PAID") {
    // Block a second click even before React renders disabled controls.
    if (inFlight.current || disabled) return;

    inFlight.current = true;
    setBusy(true);
    setMessage("");

    try {
      if (target === "PAID") {
        await staffWrite<OrderDetails>(
          `/staff/orders/${order.order_id}/paid`,
          "POST",
        );
      } else {
        const needsReason =
          target === "REJECTED" || target === "CANCELLED";

        await staffWrite<OrderDetails>(
          `/staff/orders/${order.order_id}/status`,
          "PATCH",
          {
            expected_status: order.status,
            status: target,
            ...(needsReason ? { reason: reason.trim() } : {}),
          },
        );
      }

      if (mounted.current) {
        setReason("");
        setMessage("Saved.");
      }
    } catch (error) {
      if (
        error instanceof StaffApiError &&
        [401, 403].includes(error.status)
      ) {
        onAuthError(error.message);
      } else if (mounted.current) {
        const knownRejection =
          error instanceof StaffApiError &&
          [404, 409, 422].includes(error.status);

        setMessage(
          knownRejection
            ? `${error.message} Check its current state before choosing another action.`
            : "The result could not be confirmed. Check the refreshed order before trying again.",
        );
      }
    } finally {
      inFlight.current = false;

      if (mounted.current) setBusy(false);

      // Refresh after success, conflict or timeout.
      // A request whose response was lost may still have committed.
      onChanged();
    }
  }

  if (!next) {
    return <p className="muted">This order is closed.</p>;
  }

  return (
    <section
      className="order-actions"
      aria-label="Order actions"
    >
      {message && <p role="status">{message}</p>}

      <fieldset disabled={disabled || busy}>
        <legend>Order actions</legend>

        {disabled && (
          <p className="muted">
            Waiting for a fresh connection and order details.
          </p>
        )}

        <div className="actions">
          {order.status === "READY" && unpaid ? (
            <button
              type="button"
              onClick={() => void run("PAID")}
            >
              Record payment received
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void run(next.status)}
            >
              {next.label}
            </button>
          )}
        </div>

        {order.status === "READY" && unpaid && (
          <p className="muted">
            Use this after collecting payment at pickup.
            Then complete the order.
          </p>
        )}

        {unpaid && (
          <>
            <label htmlFor={`reason-${order.order_id}`}>
              Reason for rejection or cancellation
            </label>

            <textarea
              id={`reason-${order.order_id}`}
              maxLength={300}
              rows={2}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />

            <div className="actions">
              {order.status === "PLACED" && (
                <button
                  type="button"
                  className="danger"
                  disabled={!reason.trim()}
                  onClick={() => void run("REJECTED")}
                >
                  Reject order
                </button>
              )}

              <button
                type="button"
                className="danger"
                disabled={!reason.trim()}
                onClick={() => void run("CANCELLED")}
              >
                Cancel order
              </button>
            </div>
          </>
        )}

        {busy && <p role="status">Saving…</p>}
      </fieldset>
    </section>
  );
}