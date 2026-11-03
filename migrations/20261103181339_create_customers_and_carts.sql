-- migrate:up
CREATE TABLE public.customers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164 text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (phone_e164 IS NULL OR phone_e164 ~ '^[+][1-9][0-9]{1,14}$')
);

CREATE TABLE public.carts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES public.customers(id),
    status text NOT NULL DEFAULT 'BUILDING',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN (
        'BUILDING', 'AWAITING_CONFIRMATION', 'CHECKED_OUT',
        'ABANDONED', 'HUMAN_REQUIRED'
    ))
);

CREATE TABLE public.cart_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cart_id uuid NOT NULL REFERENCES public.carts(id) ON DELETE CASCADE,
    menu_item_id uuid NOT NULL REFERENCES public.menu_items(id),
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 20),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.cart_item_modifiers (
    cart_item_id uuid NOT NULL REFERENCES public.cart_items(id) ON DELETE CASCADE,
    modifier_option_id uuid NOT NULL REFERENCES public.modifier_options(id),
    PRIMARY KEY (cart_item_id, modifier_option_id)
);

CREATE INDEX carts_customer_id_idx ON public.carts (customer_id);
CREATE INDEX cart_items_cart_id_idx ON public.cart_items (cart_id);
CREATE INDEX cart_items_menu_item_id_idx ON public.cart_items (menu_item_id);
CREATE INDEX cart_item_modifiers_option_id_idx
    ON public.cart_item_modifiers (modifier_option_id);

-- migrate:down
DROP TABLE public.cart_item_modifiers;
DROP TABLE public.cart_items;
DROP TABLE public.carts;
DROP TABLE public.customers;