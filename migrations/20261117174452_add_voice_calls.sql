-- migrate:up
CREATE TABLE public.voice_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_sid text NOT NULL,
    connection_id text NOT NULL,
    call_sid text NOT NULL,
    from_phone text,
    to_phone text NOT NULL,
    status text NOT NULL DEFAULT 'received' CHECK (status IN (
        'received', 'initiated', 'ringing', 'in-progress',
        'completed', 'busy', 'no-answer', 'canceled', 'failed'
    )),
    voice_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    UNIQUE (account_sid, call_sid)
);

CREATE TABLE public.voice_call_events (
    call_id uuid NOT NULL REFERENCES public.voice_calls(id) ON DELETE CASCADE,
    event_key text NOT NULL,
    fingerprint text NOT NULL,
    status text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (call_id, event_key)
);

-- migrate:down
DROP TABLE public.voice_call_events;
DROP TABLE public.voice_calls;