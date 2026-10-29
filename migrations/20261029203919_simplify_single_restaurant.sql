-- migrate:up
-- dbmate runs this migration in one transaction.
SET LOCAL lock_timeout = '5s';

LOCK TABLE public.restaurants, public.locations, public.location_hours
    IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF (SELECT count(*) FROM public.restaurants) > 1
       OR (SELECT count(*) FROM public.locations) > 1 THEN
        RAISE EXCEPTION
            'Single-restaurant migration requires at most one restaurant and location';
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.locations
        WHERE timezone <> 'America/Los_Angeles' OR currency <> 'USD'
    ) THEN
        RAISE EXCEPTION
            'Existing configuration must use America/Los_Angeles and USD';
    END IF;
END;
$$;

CREATE TABLE public.restaurant_settings (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    preparation_minutes integer NOT NULL DEFAULT 20
        CHECK (preparation_minutes BETWEEN 1 AND 180),
    ordering_enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.restaurant_settings (
    id, name, preparation_minutes, ordering_enabled, created_at
)
SELECT 1, r.name, COALESCE(l.preparation_minutes, 20),
       COALESCE(l.ordering_enabled, false), r.created_at
FROM public.restaurants AS r
LEFT JOIN public.locations AS l ON l.restaurant_id = r.id;

CREATE TABLE public.opening_hours (
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    opens_minute integer NOT NULL,
    closes_minute integer NOT NULL,
    PRIMARY KEY (weekday, opens_minute),
    CONSTRAINT opening_hours_valid_interval CHECK (
        opens_minute >= 0 AND closes_minute <= 1440
        AND opens_minute < closes_minute
    ),
    CONSTRAINT opening_hours_no_overlap EXCLUDE USING gist (
        weekday WITH =,
        int4range(opens_minute, closes_minute, '[)') WITH &&
    )
);

INSERT INTO public.opening_hours (weekday, opens_minute, closes_minute)
SELECT weekday, opens_minute, closes_minute
FROM public.location_hours;

-- No CASCADE: unknown dependencies must stop the migration.
DROP TABLE public.location_hours;
DROP TABLE public.locations;
DROP TABLE public.restaurants;

-- migrate:down
DO $$
BEGIN
    RAISE EXCEPTION
        'Forward-only simplification: restore the pre-migration backup and code';
END;
$$;