-- migrate:up
CREATE TABLE public.conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Each simulator conversation owns one existing cart.
    cart_id uuid NOT NULL UNIQUE
        REFERENCES public.carts(id) ON DELETE CASCADE,

    -- An unfinished add/update operation, including its observed cart version.
    draft jsonb,

    -- A persisted handoff prevents automation from restarting after a reconnect.
    staff_reason text,

    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),

    CHECK (draft IS NULL OR jsonb_typeof(draft) = 'object'),

    CHECK (
        staff_reason IS NULL
        OR length(btrim(staff_reason)) BETWEEN 1 AND 300
    ),

    CHECK (staff_reason IS NULL OR draft IS NULL)
);

-- migrate:down
DROP TABLE public.conversations;