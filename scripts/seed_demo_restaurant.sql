BEGIN;

DO $$
BEGIN
    IF current_database() <> 'restaurant_dev' THEN
        RAISE EXCEPTION 'Demo seed requires restaurant_dev';
    END IF;
END;
$$;

INSERT INTO public.restaurants (id, name)
VALUES ('a23cc9a7-6e15-43e3-bd4a-8b57dd9a3001', 'Demo Kitchen')
ON CONFLICT (id) DO NOTHING;

WITH new_location AS (
    INSERT INTO public.locations (
        id, restaurant_id, name, timezone, currency,
        preparation_minutes, ordering_enabled
    )
    VALUES (
        'cf8db1d2-7115-45e6-a994-aaadfed53001',
        'a23cc9a7-6e15-43e3-bd4a-8b57dd9a3001',
        'Main location', 'America/Los_Angeles', 'USD', 20, false
    )
    ON CONFLICT (id) DO NOTHING
    RETURNING id
)
INSERT INTO public.location_hours (
    location_id, weekday, opens_minute, closes_minute
)
SELECT new_location.id, day_number, 660, 1260
FROM new_location
CROSS JOIN generate_series(0, 6) AS days(day_number);

COMMIT;