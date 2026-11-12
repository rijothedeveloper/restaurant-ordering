import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import StaffIdentity, require_staff
from app.repositories.staff_orders import StaffOrderRepository
from app.schemas.order import OrderResponse, StaffOrderSummary
from app.schemas.staff_order import StaffOrderStatusChange
from app.services.staff_orders import (
    StaffOrderError,
    change_order_status,
    mark_order_paid,
)

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(
    prefix="/staff",
    tags=["staff"],
    dependencies=[Depends(require_staff)],
)


def action_error(error: StaffOrderError) -> HTTPException:
    return HTTPException(
        status_code=404 if error.code == "order_not_found" else 409,
        detail={
            "code": error.code,
            "message": error.message,
        },
        headers=NO_STORE,
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


@router.patch(
    "/orders/{order_id}/status",
    response_model=OrderResponse,
)
async def update_order_status(
    order_id: UUID,
    change: StaffOrderStatusChange,
    response: Response,
    staff: Annotated[StaffIdentity, Depends(require_staff)],
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> OrderResponse:
    try:
        order = await change_order_status(
            repository.connection,
            order_id=order_id,
            # Trust the verified Firebase identity, not a browser-supplied UID.
            staff_user_id=staff.user_id,
            change=change,
        )
    except StaffOrderError as error:
        raise action_error(error) from error

    response.headers.update(NO_STORE)
    return order


@router.post(
    "/orders/{order_id}/paid",
    response_model=OrderResponse,
)
async def record_order_payment(
    order_id: UUID,
    response: Response,
    staff: Annotated[StaffIdentity, Depends(require_staff)],
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> OrderResponse:
    try:
        order = await mark_order_paid(
            repository.connection,
            order_id=order_id,
            staff_user_id=staff.user_id,
        )
    except StaffOrderError as error:
        raise action_error(error) from error

    response.headers.update(NO_STORE)
    return order
