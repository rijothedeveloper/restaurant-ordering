-- migrate:up
CREATE TABLE public.modifier_groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    menu_item_id uuid NOT NULL REFERENCES public.menu_items(id),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    min_selections integer NOT NULL DEFAULT 0 CHECK (min_selections >= 0),
    max_selections integer NOT NULL DEFAULT 1 CHECK (max_selections >= 1),
    sort_order integer NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    CHECK (min_selections <= max_selections)
);

CREATE TABLE public.modifier_options (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id uuid NOT NULL REFERENCES public.modifier_groups(id),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    price_delta_cents integer NOT NULL DEFAULT 0 CHECK (price_delta_cents >= 0),
    is_available boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0 CHECK (sort_order >= 0)
);

CREATE INDEX modifier_groups_display_order
    ON public.modifier_groups (menu_item_id, sort_order, id);

CREATE INDEX modifier_options_display_order
    ON public.modifier_options (group_id, sort_order, id);

-- migrate:down
DROP TABLE public.modifier_options;
DROP TABLE public.modifier_groups;

