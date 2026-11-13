-- migrate:up

-- NULL means no kitchen browser has checked in yet.
-- Do not default this to now(): starting the API does not mean staff are present.
ALTER TABLE public.restaurant_settings
    ADD COLUMN kitchen_last_seen_at timestamptz;

-- migrate:down
ALTER TABLE public.restaurant_settings
    DROP COLUMN kitchen_last_seen_at;