import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import StaffIdentity, require_staff
from app.repositories.kitchen import KitchenRepository
from app.repositories.menu import MenuRepository
from app.repositories.menu_admin import MenuAdminRepository
from app.repositories.restaurant_config import (
    RestaurantConfigurationRepository,
)
from app.repositories.staff_orders import StaffOrderRepository
from app.schemas.kitchen import (
    AvailabilityChange,
    IntakeChange,
    KitchenOverview,
)
from app.schemas.order import OrderResponse, StaffOrderSummary
from app.schemas.staff_order import StaffOrderStatusChange
from app.services.intake import intake_block_reason, kitchen_is_online
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

@router.get("/kitchen", response_model=KitchenOverview)
async def kitchen_overview(
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> KitchenOverview:
    connection = repository.connection

    config = await RestaurantConfigurationRepository(connection).get()
    menu = await MenuRepository(connection).get()

    if config is None or menu is None:
        raise HTTPException(
            503,
            "Restaurant is not configured",
            headers=NO_STORE,
        )

    # Match the clock used by heartbeat writes and checkout.
    now = await connection.fetchval("SELECT clock_timestamp()")
    reason = intake_block_reason(config, now)

    response.headers.update(NO_STORE)

    return KitchenOverview(
        ordering_enabled=config.ordering_enabled,
        kitchen_online=kitchen_is_online(config, now),
        accepting_orders=reason is None,
        block_reason=reason,
        kitchen_last_seen_at=config.kitchen_last_seen_at,
        menu=menu,
    )


@router.post("/kitchen/heartbeat")
async def kitchen_heartbeat(
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> dict[str, bool]:
    updated = await KitchenRepository(
        repository.connection
    ).heartbeat()

    if not updated:
        raise HTTPException(
            503,
            "Restaurant is not configured",
            headers=NO_STORE,
        )

    response.headers.update(NO_STORE)
    return {"ok": True}


@router.patch("/kitchen/intake")
async def change_kitchen_intake(
    change: IntakeChange,
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> dict[str, bool]:
    updated = await KitchenRepository(
        repository.connection
    ).set_ordering_enabled(change.ordering_enabled)

    if not updated:
        raise HTTPException(
            503,
            "Restaurant is not configured",
            headers=NO_STORE,
        )

    response.headers.update(NO_STORE)
    return {"ok": True}


@router.patch("/menu/items/{item_id}/availability")
async def change_item_availability(
    item_id: UUID,
    change: AvailabilityChange,
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> dict[str, bool]:
    # Reuse the existing administrator repository.
    # This endpoint accepts availability changes only.
    updated = await MenuAdminRepository(
        repository.connection
    ).update_item(
        item_id,
        price_cents=None,
        is_available=change.is_available,
    )

    if not updated:
        raise HTTPException(
            404,
            "Menu item not found",
            headers=NO_STORE,
        )

    response.headers.update(NO_STORE)
    return {"ok": True}


@router.patch("/menu/options/{option_id}/availability")
async def change_option_availability(
    option_id: UUID,
    change: AvailabilityChange,
    response: Response,
    repository: Annotated[
        StaffOrderRepository,
        Depends(staff_orders, scope="function"),
    ],
) -> dict[str, bool]:
    updated = await MenuAdminRepository(
        repository.connection
    ).update_option(
        option_id,
        price_delta_cents=None,
        is_available=change.is_available,
    )

    if not updated:
        raise HTTPException(
            404,
            "Modifier option not found",
            headers=NO_STORE,
        )

    response.headers.update(NO_STORE)
    return {"ok": True}
