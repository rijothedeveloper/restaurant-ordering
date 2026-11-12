from uuid import UUID

import asyncpg

from app.repositories.staff_orders import StaffOrderRepository
from app.schemas.order import OrderResponse
from app.schemas.staff_order import StaffOrderStatusChange

# Closed states have no outgoing transitions.
# Payment requirements are checked separately below.
ALLOWED_TRANSITIONS = {
    "PLACED": {"ACCEPTED", "REJECTED", "CANCELLED"},
    "ACCEPTED": {"PREPARING", "CANCELLED"},
    "PREPARING": {"READY", "CANCELLED"},
    "READY": {"COMPLETED", "CANCELLED"},
}


class StaffOrderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _read_order(
    repository: StaffOrderRepository, order_id: UUID
) -> OrderResponse:
    order = await repository.get(order_id)

    if order is None:
        raise StaffOrderError("order_not_found", "Order not found")

    return order


async def change_order_status(
    connection: asyncpg.Connection,
    *,
    order_id: UUID,
    staff_user_id: str,
    change: StaffOrderStatusChange,
) -> OrderResponse:
    repository = StaffOrderRepository(connection)

    # The lock, update and audit event must succeed or roll back together.
    async with connection.transaction():
        row = await repository.lock(order_id)

        if row is None:
            raise StaffOrderError("order_not_found", "Order not found")

        # Validate the requested transition before handling repeated requests.
        if change.status not in ALLOWED_TRANSITIONS[change.expected_status]:
            raise StaffOrderError(
                "invalid_transition",
                "That transition is not allowed",
            )

        if row["status"] == change.status:
            # Already at the requested destination: return the current order.
            # Do not add another event or replace the original reason.
            return await _read_order(repository, order_id)

        if row["status"] != change.expected_status:
            raise StaffOrderError(
                "stale_order",
                "Order changed. Refresh and choose the next action.",
            )

        if (
            change.status in {"REJECTED", "CANCELLED"}
            and row["payment_status"] == "PAID"
        ):
            raise StaffOrderError(
                "refund_required",
                "This order is paid. Contact the manager about cancellation.",
            )

        if change.status == "COMPLETED" and row["payment_status"] != "PAID":
            raise StaffOrderError(
                "payment_required",
                "Record payment received before completing the order",
            )

        await repository.set_status(order_id, change.status)

        data = {
            "staff_user_id": staff_user_id,
            "from_status": row["status"],
            "to_status": change.status,
        }

        if change.reason is not None:
            data["reason"] = change.reason

        await repository.append_event(
            order_id,
            "ORDER_STATUS_CHANGED",
            data,
        )

        return await _read_order(repository, order_id)


async def mark_order_paid(
    connection: asyncpg.Connection,
    *,
    order_id: UUID,
    staff_user_id: str,
) -> OrderResponse:
    repository = StaffOrderRepository(connection)

    async with connection.transaction():
        # Use the same row lock as status changes.
        # Otherwise payment and cancellation could both succeed concurrently.
        row = await repository.lock(order_id)

        if row is None:
            raise StaffOrderError(
                "order_not_found",
                "Order not found",
            )

        if row["payment_status"] == "PAID":
            # Repeated payment recording creates no additional event.
            # This also works if the paid order was subsequently completed.
            return await _read_order(repository, order_id)

        if row["status"] != "READY":
            raise StaffOrderError(
                "not_ready",
                "Only a ready order can be marked paid at pickup",
            )
        # This records money already received; it does not charge a card.
        await repository.set_paid(order_id)
        await repository.append_event(
            order_id,
            "ORDER_PAID",
            {
                "staff_user_id": staff_user_id,
                "from_payment_status": "UNPAID",
                "to_payment_status": "PAID",
            },
        )

        return await _read_order(repository, order_id)
