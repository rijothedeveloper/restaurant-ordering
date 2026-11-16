-- migrate:up
CREATE TABLE public.tool_executions (
    conversation_id uuid NOT NULL
        REFERENCES public.conversations(id) ON DELETE CASCADE,

    execution_id uuid NOT NULL,

    -- Store the complete, consistently serialized request for comparison.
    request_json text NOT NULL,

    -- Only completed results are inserted.
    result_json jsonb NOT NULL,

    completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),

    PRIMARY KEY (conversation_id, execution_id),

    CHECK (jsonb_typeof(request_json::jsonb) = 'object'),
    CHECK (jsonb_typeof(result_json) = 'object')
);

-- migrate:down
DROP TABLE public.tool_executions;