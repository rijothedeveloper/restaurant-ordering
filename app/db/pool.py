import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from app.config import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    pool = await asyncpg.create_pool(
        dsn=settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=5,
        command_timeout=5,
    )

    app.state.db_pool = pool
    try:
        yield
    finally:
        try:
            await asyncio.wait_for(pool.close(), timeout=10)
        finally:
            app.state.db_pool = None
