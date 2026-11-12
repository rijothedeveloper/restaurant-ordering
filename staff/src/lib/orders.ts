export type OrderStatus =
  | "PLACED"
  | "ACCEPTED"
  | "PREPARING"
  | "READY"
  | "COMPLETED"
  | "REJECTED"
  | "CANCELLED";

export type OrderSummary = {
  order_id: string;
  order_number: number;
  status: OrderStatus;
  payment_status: "UNPAID" | "PAID";
  currency: "USD";
  total_cents: number;
  placed_at: string;
  pickup_at: string;
};

export type OrderDetails = OrderSummary & {
  subtotal_cents: number;
  tax_cents: number;
  items: {
    line_id: string;
    item_name: string;
    quantity: number;
    unit_price_cents: number;
    line_total_cents: number;
    options: {
      option_id: string;
      name: string;
      price_delta_cents: number;
    }[];
  }[];
};

const moneyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

const timeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
  timeZoneName: "short",
});

export const money = (cents: number) =>
  moneyFormatter.format(cents / 100);

export const localTime = (value: string | number) =>
  timeFormatter.format(new Date(value));

export const statusLabel = (value: string) =>
  value.charAt(0) + value.slice(1).toLowerCase();