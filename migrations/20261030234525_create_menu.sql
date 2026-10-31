-- migrate:up
CREATE TABLE public.menu_categories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    sort_order integer NOT NULL DEFAULT 0 CHECK (sort_order >= 0)
);

CREATE TABLE public.menu_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id uuid NOT NULL REFERENCES public.menu_categories(id),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    description text NOT NULL DEFAULT '',
    price_cents integer NOT NULL CHECK (price_cents >= 0),
    is_available boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0 CHECK (sort_order >= 0)
);

CREATE INDEX menu_categories_display_order
    ON public.menu_categories (sort_order, id);

CREATE INDEX menu_items_display_order
    ON public.menu_items (category_id, sort_order, id);

-- migrate:down
DROP TABLE public.menu_items;
DROP TABLE public.menu_categories;