-- migrate:up
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE public.locations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id uuid NOT NULL REFERENCES public.restaurants(id),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    timezone text NOT NULL CHECK (length(btrim(timezone)) > 0),
    currency text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    preparation_minutes integer NOT NULL DEFAULT 20
        CHECK (preparation_minutes BETWEEN 1 AND 180),
    ordering_enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, id)
);

CREATE TABLE public.location_hours (
    location_id uuid NOT NULL
        REFERENCES public.locations(id) ON DELETE CASCADE,
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    opens_minute integer NOT NULL,
    closes_minute integer NOT NULL,
    PRIMARY KEY (location_id, weekday, opens_minute),
    CONSTRAINT location_hours_valid_interval CHECK (
        opens_minute >= 0
        AND closes_minute <= 1440
        AND opens_minute < closes_minute
    ),
    CONSTRAINT location_hours_no_overlap EXCLUDE USING gist (
        location_id WITH =,
        weekday WITH =,
        int4range(opens_minute, closes_minute, '[)') WITH &&
    )
);

-- migrate:down
DROP TABLE public.location_hours;
DROP TABLE public.locations;
-- Keep the shared btree_gist extension installed.