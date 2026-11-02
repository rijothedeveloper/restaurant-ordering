from uuid import UUID

import pytest

from app.schemas.menu import (
    MenuCategory,
    MenuItem,
    MenuResponse,
    ModifierGroup,
    ModifierOption,
)
from app.schemas.selection import SelectionRequest
from app.services.menu_selection import SelectionError, validate_selection


def uid(value: int) -> UUID:
    return UUID(int=value)


@pytest.fixture
def menu() -> MenuResponse:
    def option(
        number: int,
        name: str,
        price: int,
        available: bool = True,
    ) -> ModifierOption:
        return ModifierOption(
            id=uid(number),
            name=name,
            price_delta_cents=price,
            is_available=available,
        )

    bowl = MenuItem(
        id=uid(1),
        name="Bowl",
        description="",
        price_cents=1399,
        is_available=True,
        modifier_groups=[
            ModifierGroup(
                id=uid(11),
                name="Size",
                min_selections=1,
                max_selections=1,
                options=[
                    option(101, "Regular", 0),
                    option(102, "Large", 300),
                ],
            ),
            ModifierGroup(
                id=uid(12),
                name="Extras",
                min_selections=0,
                max_selections=2,
                options=[
                    option(103, "Chicken", 400),
                    option(104, "Avocado", 200),
                    option(105, "Cheese", 150, False),
                    option(106, "Sauce", 50),
                ],
            ),
        ],
    )

    plain = MenuItem(
        id=uid(2),
        name="Plain",
        description="",
        price_cents=999,
        is_available=True,
    )

    unavailable = MenuItem(
        id=uid(3),
        name="Sold out",
        description="",
        price_cents=999,
        is_available=False,
    )

    other = MenuItem(
        id=uid(4),
        name="Other",
        description="",
        price_cents=500,
        is_available=True,
        modifier_groups=[
            ModifierGroup(
                id=uid(13),
                name="Other extras",
                min_selections=0,
                max_selections=1,
                options=[option(201, "Other option", 25)],
            )
        ],
    )

    return MenuResponse(
        restaurant_name="Test Kitchen",
        ordering_enabled=False,
        categories=[
            MenuCategory(
                id=uid(10),
                name="Mains",
                items=[bowl, plain, unavailable, other],
            )
        ],
    )


@pytest.mark.parametrize(
    "item,choices,price,ordered",
    [
        (1, [101], 1399, [101]),
        (1, [103, 102], 2099, [102, 103]),
        (1, [104, 101, 103], 1999, [101, 103, 104]),
        (2, [], 999, []),
        (4, [201], 525, [201]),
    ],
)
def test_valid_selection(
    menu: MenuResponse,
    item: int,
    choices: list[int],
    price: int,
    ordered: list[int],
) -> None:
    result = validate_selection(
        menu,
        SelectionRequest(
            item_id=uid(item),
            option_ids=[uid(x) for x in choices],
        ),
    )

    assert result.item_id == uid(item)
    assert result.unit_price_cents == price
    assert result.currency == "USD"
    assert [option.id for option in result.options] == [uid(x) for x in ordered]


@pytest.mark.parametrize(
    "item,choices,code",
    [
        (999, [], "item_not_found"),
        (3, [], "item_unavailable"),
        (1, [], "too_few_options"),
        (1, [101, 102], "too_many_options"),
        (1, [101, 103, 104, 106], "too_many_options"),
        (1, [101, 105], "option_unavailable"),
        (1, [101, 101], "duplicate_option"),
        (1, [201], "invalid_option"),
        (2, [101], "invalid_option"),
        (1, [999], "invalid_option"),
    ],
)
def test_invalid_selection(
    menu: MenuResponse,
    item: int,
    choices: list[int],
    code: str,
) -> None:
    with pytest.raises(SelectionError) as error:
        validate_selection(
            menu,
            SelectionRequest(
                item_id=uid(item),
                option_ids=[uid(x) for x in choices],
            ),
        )

    assert error.value.code == code


def test_empty_required_group_is_not_ignored(menu: MenuResponse) -> None:
    menu.categories[0].items[0].modifier_groups[0].options = []

    with pytest.raises(SelectionError) as error:
        validate_selection(menu, SelectionRequest(item_id=uid(1)))

    assert error.value.code == "too_few_options"


def test_validation_preserves_inputs_and_canonicalizes_order(
    menu: MenuResponse,
) -> None:
    before = menu.model_dump()

    selection = SelectionRequest(
        item_id=uid(1),
        option_ids=[uid(103), uid(102)],
    )
    first = validate_selection(menu, selection)

    second = validate_selection(
        menu,
        SelectionRequest(
            item_id=uid(1),
            option_ids=[uid(102), uid(103)],
        ),
    )

    assert first == second
    assert selection.option_ids == [uid(103), uid(102)]
    assert menu.model_dump() == before
