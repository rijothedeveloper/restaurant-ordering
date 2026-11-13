from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Match the existing PostgreSQL bigint cart-version range.
CartVersion = Annotated[int, Field(ge=0, lt=2**63)]


class ToolArguments(BaseModel):
    # Reject unknown fields and coercions such as "2" or true into integers.
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyArguments(ToolArguments):
    pass


class SearchMenuArguments(ToolArguments):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]


class CartVersionArguments(ToolArguments):
    expected_version: CartVersion


class AddCartItemArguments(CartVersionArguments):
    item_id: UUID
    quantity: int = Field(ge=1, le=20)

    # Required even when empty: the caller must explicitly choose options.
    option_ids: list[UUID]


class UpdateCartItemArguments(CartVersionArguments):
    line_id: UUID
    quantity: int = Field(ge=1, le=20)

    # This replaces the complete selection; it is not a partial update.
    option_ids: list[UUID]


class RemoveCartItemArguments(CartVersionArguments):
    line_id: UUID


class PlaceOrderArguments(ToolArguments):
    quote_id: UUID


class RequestStaffArguments(ToolArguments):
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ]


class ToolError(BaseModel):
    code: str
    message: str
    fields: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None


# This allowlist supplies both argument validation and advertised schemas.
FUNCTIONS: dict[str, tuple[type[ToolArguments], str]] = {
    "get_menu": (
        EmptyArguments,
        "Read the menu, prices in cents, modifier rules, and availability flags. "
        "Item availability alone does not mean ordering is open.",
    ),
    "search_menu": (
        SearchMenuArguments,
        "Find available items by name or description. Multiple matches require "
        "clarification; never guess an item or modifier ID.",
    ),
    "get_cart": (EmptyArguments, "Read this conversation's cart and current version."),
    "add_cart_item": (
        AddCartItemArguments,
        "Add a separate line using menu IDs and the last observed cart version. "
        "Return the added line and new version. Prices come from the server.",
    ),
    "update_cart_item": (
        UpdateCartItemArguments,
        "Replace a line's quantity and complete option selection using the last "
        "observed cart version. Return the updated cart.",
    ),
    "remove_cart_item": (
        RemoveCartItemArguments,
        "Remove a line using the last observed cart version. Return the updated cart.",
    ),
    "create_quote": (
        CartVersionArguments,
        "Create a quote for the observed cart version. It includes the total, "
        "expiry, and ASAP pickup time. A quote is not an order or confirmation.",
    ),
    "place_order": (
        PlaceOrderArguments,
        "Place a quote only after the application recorded customer confirmation. "
        "The server rechecks checkout conditions. Payment remains due at pickup.",
    ),
    "request_staff": (
        RequestStaffArguments,
        "Stop automated actions and request staff assistance. This request alone "
        "does not mean a phone transfer has started or connected.",
    ),
}


def function_schemas() -> list[dict[str, Any]]:
    # These are application contracts; a provider adapter can wrap them later.
    return [
        {
            "name": name,
            "description": description,
            "parameters": arguments.model_json_schema(),
        }
        for name, (arguments, description) in FUNCTIONS.items()
    ]