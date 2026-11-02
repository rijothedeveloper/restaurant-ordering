import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response

from app.repositories.menu import MenuRepository
from app.schemas.menu import MenuResponse
from app.schemas.selection import SelectionRequest, ValidatedSelection
from app.services.menu_selection import SelectionError, validate_selection

router = APIRouter(prefix="/menu", tags=["menu"])
NO_STORE = {"Cache-Control": "no-store"}


async def load_menu(request: Request) -> MenuResponse:
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

    return menu


@router.get(
    "",
    response_model=MenuResponse,
    responses={503: {"description": "Restaurant settings or database unavailable"}},
)
async def get_menu(request: Request, response: Response) -> MenuResponse:
    menu = await load_menu(request)
    response.headers.update(NO_STORE)
    return menu


@router.post(
    "/validate-selection",
    response_model=ValidatedSelection,
    responses={
        404: {"description": "Item not found"},
        422: {"description": "Invalid selection or request body"},
        503: {"description": "Restaurant settings or database unavailable"},
    },
)
async def check_selection(
    selection: SelectionRequest,
    request: Request,
    response: Response,
) -> ValidatedSelection:
    menu = await load_menu(request)

    try:
        result = validate_selection(menu, selection)
    except SelectionError as exc:
        status = 404 if exc.code == "item_not_found" else 422

        raise HTTPException(
            status,
            {"code": exc.code, "message": str(exc)},
            headers=NO_STORE,
        ) from exc

    response.headers.update(NO_STORE)
    return result
