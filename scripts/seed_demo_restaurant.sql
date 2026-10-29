BEGIN;

DO $$
BEGIN
    IF current_database() <> 'restaurant_dev' THEN
        RAISE EXCEPTION 'Demo seed requires restaurant_dev';
    END IF;
END;
$$;

WITH new_settings AS (
    INSERT INTO public.restaurant_settings (id, name)
    VALUES (1, 'Demo Kitchen')
    ON CONFLICT (id) DO NOTHING
    RETURNING id
)
INSERT INTO public.opening_hours (weekday, opens_minute, closes_minute)
SELECT day_number, 660, 1260
FROM new_settings
CROSS JOIN generate_series(0, 6) AS days(day_number);

COMMIT;