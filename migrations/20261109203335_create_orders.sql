-- migrate:up
CREATE TABLE public.orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    cart_id uuid NOT NULL UNIQUE REFERENCES public.carts(id),
    quote_id uuid NOT NULL REFERENCES public.quotes(id),
    customer_id uuid NOT NULL REFERENCES public.customers(id),
    cart_version bigint NOT NULL CHECK (cart_version >= 0),
    status text NOT NULL DEFAULT 'PLACED' CHECK (status IN (
        'PLACED', 'ACCEPTED', 'PREPARING', 'READY', 'COMPLETED',
        'REJECTED', 'CANCELLED'
    )),
    payment_status text NOT NULL DEFAULT 'UNPAID'
        CHECK (payment_status IN ('UNPAID', 'PAID')),
    currency text NOT NULL CHECK (currency = 'USD'),
    subtotal_cents bigint NOT NULL CHECK (subtotal_cents >= 0),
    tax_rate_bps integer NOT NULL CHECK (tax_rate_bps BETWEEN 0 AND 10000),
    tax_cents bigint NOT NULL CHECK (tax_cents >= 0),
    total_cents bigint NOT NULL CHECK (total_cents = subtotal_cents + tax_cents),
    confirmed_at timestamptz NOT NULL,
    placed_at timestamptz NOT NULL CHECK (placed_at >= confirmed_at),
    pickup_at timestamptz NOT NULL CHECK (pickup_at > placed_at)
);

CREATE INDEX orders_customer_id_idx ON public.orders(customer_id);
CREATE INDEX orders_quote_id_idx ON public.orders(quote_id);

CREATE TABLE public.order_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id uuid NOT NULL REFERENCES public.orders(id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    source_line_id uuid NOT NULL,
    menu_item_id uuid NOT NULL,
    item_name text NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 20),
    unit_price_cents bigint NOT NULL CHECK (unit_price_cents >= 0),
    line_total_cents bigint NOT NULL
        CHECK (line_total_cents = unit_price_cents * quantity),
    UNIQUE (order_id, position)
);

CREATE TABLE public.order_item_modifiers (
    order_item_id uuid NOT NULL REFERENCES public.order_items(id) ON DELETE CASCADE,
    modifier_option_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    name text NOT NULL,
    price_delta_cents bigint NOT NULL CHECK (price_delta_cents >= 0),
    PRIMARY KEY (order_item_id, modifier_option_id),
    UNIQUE (order_item_id, position)
);

CREATE TABLE public.order_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id uuid NOT NULL REFERENCES public.orders(id) ON DELETE CASCADE,
    event_type text NOT NULL CHECK (length(btrim(event_type)) > 0),
    data jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(data) = 'object'),
    occurred_at timestamptz NOT NULL
);

CREATE INDEX order_events_order_id_idx
    ON public.order_events(order_id, id);

-- migrate:down
DROP TABLE public.order_events;
DROP TABLE public.order_item_modifiers;
DROP TABLE public.order_items;
DROP TABLE public.orders;