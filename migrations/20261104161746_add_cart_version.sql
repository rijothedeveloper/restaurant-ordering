-- migrate:up
ALTER TABLE public.carts
    ADD COLUMN version bigint NOT NULL DEFAULT 0
    CONSTRAINT carts_version_nonnegative CHECK (version >= 0);

-- migrate:down
ALTER TABLE public.carts DROP COLUMN version;