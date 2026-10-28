import asyncio

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/", tags=["system"])
async def root() -> dict[str, str]:
    return {
        "service": "restaurant-ordering",
        "message": "API is running",
    }


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    tags=["system"],
    responses={503: {"description": "Database unavailable"}},
)
async def ready(request: Request) -> JSONResponse:
    pool = request.app.state.db_pool
    available = False

    if pool is not None:
        try:
            async with asyncio.timeout(2):
                async with pool.acquire(timeout=1) as connection:
                    await connection.fetchval("SELECT 1", timeout=1)
            available = True
        except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            available = False

    return JSONResponse(
        status_code=200 if available else 503,
        content={"status": "ready" if available else "not_ready"},
        headers={"Cache-Control": "no-store"},
    )
