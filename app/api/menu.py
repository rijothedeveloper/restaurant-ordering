import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response

from app.repositories.menu import MenuRepository
from app.schemas.menu import MenuResponse

router = APIRouter(prefix="/menu", tags=["menu"])
NO_STORE = {"Cache-Control": "no-store"}


@router.get(
    "",
    response_model=MenuResponse,
    responses={503: {"description": "Restaurant settings or database unavailable"}},
)
async def get_menu(request: Request, response: Response) -> MenuResponse:
    pool = request.app.state.db_pool

    if pool is None:
        raise HTTPException(
            503,
            "Menu temporarily unavailable",
            headers=NO_STORE,
        )

    try:
        async with pool.acquire(timeout=5) as connection:
            menu = await MenuRepository(connection).get()
    except (
        TimeoutError,
        OSError,
        asyncpg.PostgresConnectionError,
        asyncpg.InterfaceError,
    ) as exc:
        raise HTTPException(
            503,
            "Menu temporarily unavailable",
            headers=NO_STORE,
        ) from exc

    if menu is None:
        raise HTTPException(
            503,
            "Restaurant is not configured",
            headers=NO_STORE,
        )

    response.headers.update(NO_STORE)
    return menu
