BEGIN;

DO $$
BEGIN
    IF current_database() <> 'restaurant_dev' THEN
        RAISE EXCEPTION 'Demo seed requires restaurant_dev';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.restaurant_settings
        WHERE id = 1
    ) THEN
        RAISE EXCEPTION 'Run seed_demo_restaurant.sql first';
    END IF;
END;
$$;

INSERT INTO public.menu_categories (id, name, sort_order)
VALUES
    ('70a9dd3e-20ce-4e56-91ee-e98df8f43001', 'Mains', 10),
    ('70a9dd3e-20ce-4e56-91ee-e98df8f43002', 'Drinks', 20)
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.menu_items (
    id,
    category_id,
    name,
    description,
    price_cents,
    is_available,
    sort_order
)
VALUES
    (
        '3058d7c7-38de-4fbb-a1dd-10465c8e4001',
        '70a9dd3e-20ce-4e56-91ee-e98df8f43001',
        'Grilled Chicken Bowl',
        'Chicken served with rice.',
        1399,
        true,
        10
    ),
    (
        '3058d7c7-38de-4fbb-a1dd-10465c8e4002',
        '70a9dd3e-20ce-4e56-91ee-e98df8f43001',
        'Vegetable Rice Bowl',
        'Mixed vegetables served with rice.',
        1199,
        true,
        20
    ),
    (
        '3058d7c7-38de-4fbb-a1dd-10465c8e4003',
        '70a9dd3e-20ce-4e56-91ee-e98df8f43002',
        'Lemonade',
        'A chilled lemon drink.',
        299,
        true,
        10
    ),
    (
        '3058d7c7-38de-4fbb-a1dd-10465c8e4004',
        '70a9dd3e-20ce-4e56-91ee-e98df8f43002',
        'Mango Lassi',
        'A mango yogurt drink.',
        399,
        false,
        20
    )
ON CONFLICT (id) DO NOTHING;

COMMIT;