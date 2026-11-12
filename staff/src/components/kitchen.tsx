"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { useStaffPolling } from "@/hooks/use-staff-polling";
import {
  localTime,
  money,
  statusLabel,
  type OrderDetails,
  type OrderSummary,
} from "@/lib/orders";

function beep(context: AudioContext) {
  if (context.state !== "running") return;

  const oscillator = context.createOscillator();
  const gain = context.createGain();

  oscillator.frequency.value = 880;
  gain.gain.value = 0.06;

  oscillator.connect(gain);
  gain.connect(context.destination);

  oscillator.start();
  oscillator.stop(context.currentTime + 0.18);

  oscillator.onended = () => {
    oscillator.disconnect();
    gain.disconnect();
  };
}

export default function Kitchen({
  onSignOut,
  signingOut,
  signOutError,
}: {
  onSignOut: () => Promise<void>;
  signingOut: boolean;
  signOutError: string;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [denied, setDenied] = useState("");
  const [unread, setUnread] = useState<Set<string>>(new Set());
  const [sound, setSound] = useState(false);
  const [soundError, setSoundError] = useState("");
  const [now, setNow] = useState(0);

  const seen = useRef<Set<string> | null>(null);
  const audio = useRef<AudioContext | null>(null);

  const denyAccess = useCallback(
    (message: string) => setDenied(message),
    [],
  );

  const receiveOrders = useCallback(
    (orders: OrderSummary[]) => {
      const previous = seen.current;

      const added = previous
        ? orders.filter(
            (order) => !previous.has(order.order_id),
          )
        : [];

      seen.current = new Set([
        ...(previous ?? []),
        ...orders.map((order) => order.order_id),
      ]);

      if (added.length) {
        setUnread(
          (old) =>
            new Set([
              ...old,
              ...added.map((order) => order.order_id),
            ]),
        );

        if (audio.current) {
          try {
            beep(audio.current);
          } catch {
            // A sound failure must not prevent displaying the order.
          }
        }
      }
    },
    [],
  );

  const queue = useStaffPolling<OrderSummary[]>(
    denied ? null : "/staff/orders",
    denyAccess,
    receiveOrders,
  );

  const detail = useStaffPolling<OrderDetails>(
    denied || !selected
      ? null
      : `/staff/orders/${selected}`,
    denyAccess,
  );

  const orders = queue.data ?? [];

  const newCount = orders.filter(
    (order) => unread.has(order.order_id),
  ).length;

  const stale =
    !queue.updatedAt ||
    !!queue.error ||
    now - queue.updatedAt > 10_000;

  const detailStale =
    !detail.updatedAt ||
    !!detail.error ||
    now - detail.updatedAt > 10_000;

  useEffect(() => {
    const timer = setInterval(
      () => setNow(Date.now()),
      1000,
    );

    return () => {
      clearInterval(timer);
      void audio.current?.close();
    };
  }, []);

  useEffect(() => {
    document.title = newCount
      ? `${newCount} new orders · Restaurant kitchen`
      : "Restaurant kitchen";

    return () => {
      document.title = "Restaurant kitchen";
    };
  }, [newCount]);

  async function toggleSound() {
    setSoundError("");

    try {
      if (sound) {
        await audio.current?.suspend();
        setSound(false);
      } else {
        audio.current ??= new AudioContext();
        await audio.current.resume();

        setSound(true);
        beep(audio.current);
      }
    } catch {
      setSound(false);
      setSoundError(
        "Sound is unavailable. Visual alerts remain enabled.",
      );
    }
  }

  function choose(orderId: string) {
    setSelected(orderId);

    setUnread((old) => {
      const next = new Set(old);
      next.delete(orderId);
      return next;
    });
  }

  return (
    <main className="kitchen">
      <header className="kitchen-header">
        <div>
          <p className="eyebrow">RESTAURANT STAFF</p>
          <h1>Kitchen orders</h1>
        </div>

        <div className="actions">
          {!denied && (
            <button
              className="secondary"
              onClick={toggleSound}
            >
              {sound ? "Mute sound" : "Enable sound"}
            </button>
          )}

          <button
            onClick={onSignOut}
            disabled={signingOut}
          >
            Sign out
          </button>
        </div>
      </header>

      {signOutError && (
        <p role="alert">{signOutError}</p>
      )}

      {soundError && (
        <p role="status">{soundError}</p>
      )}

      {denied ? (
        <p role="alert">{denied}</p>
      ) : (
        <>
          <section
            className={`connection ${stale ? "stale" : "live"}`}
            aria-live="polite"
          >
            <strong>
              {queue.error
                ? "Connection lost — retrying"
                : stale
                  ? "Connecting / checking orders"
                  : "Connected"}
            </strong>

            <span>
              {queue.updatedAt
                ? `Last updated ${localTime(queue.updatedAt)}`
                : "Waiting for the first update."}
            </span>

            {stale && queue.data && (
              <span>
                Showing the last successful update.
                Orders may have changed.
              </span>
            )}
          </section>

          <p role="status">
            {newCount
              ? `${newCount} new order${newCount === 1 ? "" : "s"}. Select an order to clear its alert.`
              : "No new order alerts."}
          </p>

          <div className="kitchen-grid">
            <section
              className="panel"
              aria-labelledby="queue-title"
            >
              <h2 id="queue-title">
                Active orders ({orders.length})
              </h2>

              {!queue.data && (
                <p>{queue.error || "Loading orders…"}</p>
              )}

              {queue.data && orders.length === 0 && (
                <p>No active orders.</p>
              )}

              <ul className="order-list">
                {orders.map((order) => (
                  <li key={order.order_id}>
                    <button
                      className={`order-card ${
                        unread.has(order.order_id)
                          ? "new-order"
                          : ""
                      }`}
                      aria-pressed={
                        selected === order.order_id
                      }
                      onClick={() => choose(order.order_id)}
                    >
                      <span className="order-heading">
                        <strong>
                          #{order.order_number}
                        </strong>

                        {unread.has(order.order_id) && (
                          <span className="badge">New</span>
                        )}
                      </span>

                      <span>
                        {statusLabel(order.status)} ·{" "}
                        {statusLabel(order.payment_status)}
                      </span>

                      <span>
                        Placed {localTime(order.placed_at)}
                      </span>

                      <span>
                        Pickup {localTime(order.pickup_at)}
                      </span>

                      <strong>
                        {money(order.total_cents)}
                      </strong>
                    </button>
                  </li>
                ))}
              </ul>
            </section>

            <section
              className="panel"
              aria-labelledby="details-title"
            >
              <h2 id="details-title">Order details</h2>

              {!selected && (
                <p>Select an order from the queue.</p>
              )}

              {selected && !detail.data && (
                <p role={detail.error ? "alert" : "status"}>
                  {detail.error || "Loading order…"}
                </p>
              )}

              {selected && detail.data && (
                <>
                  {detailStale && (
                    <p
                      className="connection stale"
                      role="alert"
                    >
                      Details may be outdated.{" "}
                      {detail.error ||
                        "Checking for an update."}
                    </p>
                  )}

                  <h3>
                    Order #{detail.data.order_number}
                  </h3>

                  <p>
                    {statusLabel(detail.data.status)} ·{" "}
                    {detail.data.payment_status === "UNPAID"
                      ? "Pay at pickup"
                      : "Paid"}
                  </p>

                  <p>
                    Pickup: {localTime(detail.data.pickup_at)}
                  </p>

                  <ul className="detail-items">
                    {detail.data.items.map((item) => (
                      <li key={item.line_id}>
                        <div className="order-heading">
                          <strong>
                            {item.quantity} × {item.item_name}
                          </strong>

                          <strong>
                            {money(item.line_total_cents)}
                          </strong>
                        </div>

                        <span>
                          {money(item.unit_price_cents)} each
                        </span>

                        {item.options.length > 0 && (
                          <ul>
                            {item.options.map((option) => (
                              <li key={option.option_id}>
                                {option.name}
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>

                  <dl className="totals">
                    <dt>Subtotal</dt>
                    <dd>
                      {money(detail.data.subtotal_cents)}
                    </dd>

                    <dt>Tax</dt>
                    <dd>{money(detail.data.tax_cents)}</dd>

                    <dt>Total</dt>
                    <dd>
                      <strong>
                        {money(detail.data.total_cents)}
                      </strong>
                    </dd>
                  </dl>

                  <p className="muted">
                    Item prices include selected modifiers.
                    All times are restaurant local time.
                  </p>
                </>
              )}
            </section>
          </div>
        </>
      )}
    </main>
  );
}