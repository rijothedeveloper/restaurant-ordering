import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import StaffIdentity, require_staff
from app.repositories.staff_orders import StaffOrderRepository
from app.schemas.order import OrderResponse, StaffOrderSummary

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(
    prefix="/staff",
    tags=["staff"],
    dependencies=[Depends(require_staff)],
)


async def staff_orders(
    request: Request,
) -> AsyncIterator[StaffOrderRepository]:
    pool = request.app.state.db_pool

    if pool is None:
        raise HTTPException(
            503,
            "Kitchen orders unavailable",
            headers=NO_STORE,
        )

    try:
        async with pool.acquire(timeout=5) as connection:
            yield StaffOrderRepository(connection)
    except (
        TimeoutError,
        OSError,
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
    ) as error:
        logger.warning(
            "Kitchen database unavailable: %s",
            type(error).__name__,
        )
        raise HTTPException(
            503,
            "Kitchen orders unavailable",
            headers=NO_STORE,
        ) from error


@router.get("/me", response_model=StaffIdentity)
async def staff_me(
    response: Response,
    staff: Annotated[StaffIdentity, Depends(require_staff)],
) -> StaffIdentity:
    response.headers.update(NO_STORE)
    return staff


@router.get("/orders", response_model=list[StaffOrderSummary])
async def active_orders(
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> list[StaffOrderSummary]:
    response.headers.update(NO_STORE)
    return await repository.list_active()


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def order_details(
    order_id: UUID,
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> OrderResponse:
    order = await repository.get(order_id)

    if order is None:
        raise HTTPException(
            404,
            "Order not found",
            headers=NO_STORE,
        )

    response.headers.update(NO_STORE)
    return order
