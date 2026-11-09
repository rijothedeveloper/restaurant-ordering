-- migrate:up
ALTER TABLE public.quotes
    ADD COLUMN is_pending boolean NOT NULL DEFAULT false,
    ADD COLUMN readback_token uuid,
    ADD COLUMN readback_completed_at timestamptz,
    ADD COLUMN confirmed_at timestamptz,
    ADD CONSTRAINT quote_readback_pair CHECK (
        (readback_token IS NULL) = (readback_completed_at IS NULL)
    ),
    ADD CONSTRAINT quote_readback_time CHECK (
        readback_completed_at IS NULL OR (
            readback_completed_at >= created_at
            AND readback_completed_at < expires_at
        )
    ),
    ADD CONSTRAINT quote_confirmation_time CHECK (
        confirmed_at IS NULL OR (
            readback_completed_at IS NOT NULL
            AND confirmed_at >= readback_completed_at
            AND confirmed_at < expires_at
        )
    );

CREATE UNIQUE INDEX quotes_one_pending_per_cart_idx
    ON public.quotes(cart_id) WHERE is_pending;

-- migrate:down
DROP INDEX public.quotes_one_pending_per_cart_idx;

ALTER TABLE public.quotes
    DROP CONSTRAINT quote_confirmation_time,
    DROP CONSTRAINT quote_readback_time,
    DROP CONSTRAINT quote_readback_pair,
    DROP COLUMN confirmed_at,
    DROP COLUMN readback_completed_at,
    DROP COLUMN readback_token,
    DROP COLUMN is_pending;