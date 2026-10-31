BEGIN;

DO $$
BEGIN
    IF current_database() <> 'restaurant_dev' THEN
        RAISE EXCEPTION 'Demo seed requires restaurant_dev';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.menu_items
        WHERE id = '3058d7c7-38de-4fbb-a1dd-10465c8e4001'
    ) THEN
        RAISE EXCEPTION 'Run seed_demo_menu.sql first';
    END IF;
END;
$$;

INSERT INTO public.modifier_groups (
    id,
    menu_item_id,
    name,
    min_selections,
    max_selections,
    sort_order
)
VALUES
    (
        '8226bea1-3c24-48a8-b497-a4bb9b8c5001',
        '3058d7c7-38de-4fbb-a1dd-10465c8e4001',
        'Size',
        1,
        1,
        10
    ),
    (
        '8226bea1-3c24-48a8-b497-a4bb9b8c5002',
        '3058d7c7-38de-4fbb-a1dd-10465c8e4001',
        'Extras',
        0,
        2,
        20
    )
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.modifier_options (
    id,
    group_id,
    name,
    price_delta_cents,
    is_available,
    sort_order
)
VALUES
    (
        '0586c267-87c7-49b3-9143-7ef03f406001',
        '8226bea1-3c24-48a8-b497-a4bb9b8c5001',
        'Regular',
        0,
        true,
        10
    ),
    (
        '0586c267-87c7-49b3-9143-7ef03f406002',
        '8226bea1-3c24-48a8-b497-a4bb9b8c5001',
        'Large',
        300,
        true,
        20
    ),
    (
        '0586c267-87c7-49b3-9143-7ef03f406003',
        '8226bea1-3c24-48a8-b497-a4bb9b8c5002',
        'Extra chicken',
        400,
        true,
        10
    ),
    (
        '0586c267-87c7-49b3-9143-7ef03f406004',
        '8226bea1-3c24-48a8-b497-a4bb9b8c5002',
        'Avocado',
        200,
        true,
        20
    ),
    (
        '0586c267-87c7-49b3-9143-7ef03f406005',
        '8226bea1-3c24-48a8-b497-a4bb9b8c5002',
        'Cheese',
        150,
        false,
        30
    )
ON CONFLICT (id) DO NOTHING;

COMMIT;