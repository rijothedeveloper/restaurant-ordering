-- migrate:up
CREATE TABLE public.voice_relay_sessions (
    id uuid PRIMARY KEY,
    call_id uuid NOT NULL UNIQUE
        REFERENCES public.voice_calls(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    provider_session_id text,
    closed_at timestamptz
);

-- migrate:down
DROP TABLE public.voice_relay_sessions;