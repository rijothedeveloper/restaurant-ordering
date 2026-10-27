-- migrate:up
CREATE TABLE public.restaurants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT restaurants_name_not_blank
        CHECK (length(btrim(name)) > 0)
);

-- migrate:down
DROP TABLE public.restaurants;

