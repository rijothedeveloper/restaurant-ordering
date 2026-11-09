-- migrate:up
ALTER TABLE public.restaurant_settings
    ADD COLUMN tax_rate_bps integer
    CONSTRAINT restaurant_tax_rate_range
        CHECK (tax_rate_bps BETWEEN 0 AND 10000);

CREATE TABLE public.quotes (
    id uuid PRIMARY KEY,
    cart_id uuid NOT NULL REFERENCES public.carts(id) ON DELETE CASCADE,
    cart_version bigint NOT NULL CHECK (cart_version >= 0),
    currency text NOT NULL CHECK (currency = 'USD'),

    items jsonb NOT NULL
        CHECK (jsonb_typeof(items) = 'array' AND items <> '[]'::jsonb),

    subtotal_cents bigint NOT NULL CHECK (subtotal_cents >= 0),
    tax_rate_bps integer NOT NULL CHECK (tax_rate_bps BETWEEN 0 AND 10000),
    tax_cents bigint NOT NULL CHECK (tax_cents >= 0),
    total_cents bigint NOT NULL CHECK (total_cents = subtotal_cents + tax_cents),

    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    pickup_at timestamptz NOT NULL CHECK (pickup_at > expires_at)
);

CREATE INDEX quotes_cart_id_idx ON public.quotes(cart_id);

-- migrate:down
DROP TABLE public.quotes;
ALTER TABLE public.restaurant_settings DROP COLUMN tax_rate_bps;