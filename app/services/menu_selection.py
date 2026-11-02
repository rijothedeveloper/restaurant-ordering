from app.schemas.menu import MenuResponse, ModifierOption
from app.schemas.selection import SelectionRequest, ValidatedSelection


class SelectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_selection(
    menu: MenuResponse, selection: SelectionRequest
) -> ValidatedSelection:
    item = next(
        (
            item
            for category in menu.categories
            for item in category.items
            if item.id == selection.item_id
        ),
        None,
    )

    if item is None:
        raise SelectionError("item_not_found", "Menu item was not found.")

    if not item.is_available:
        raise SelectionError("item_unavailable", "This item is unavailable.")

    selected_ids = set(selection.option_ids)

    if len(selected_ids) != len(selection.option_ids):
        raise SelectionError(
            "duplicate_option",
            "Select each option only once.",
        )

    allowed = {
        option.id: option for group in item.modifier_groups for option in group.options
    }

    for option_id in selection.option_ids:
        option = allowed.get(option_id)

        if option is None:
            raise SelectionError(
                "invalid_option",
                "An option does not belong to this item.",
            )

        if not option.is_available:
            raise SelectionError(
                "option_unavailable",
                f"{option.name} is unavailable.",
            )

    options: list[ModifierOption] = []

    for group in item.modifier_groups:
        chosen = [option for option in group.options if option.id in selected_ids]

        if len(chosen) < group.min_selections:
            raise SelectionError(
                "too_few_options",
                f"{group.name}: choose at least {group.min_selections} option(s).",
            )

        if len(chosen) > group.max_selections:
            raise SelectionError(
                "too_many_options",
                f"{group.name}: choose at most {group.max_selections} option(s).",
            )

        options.extend(chosen)

    return ValidatedSelection(
        item_id=item.id,
        item_name=item.name,
        options=options,
        unit_price_cents=(
            item.price_cents + sum(option.price_delta_cents for option in options)
        ),
    )
