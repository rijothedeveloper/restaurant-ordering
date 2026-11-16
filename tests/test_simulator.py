import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr

from app.cli import simulator as cli
from app.config import Settings
from app.repositories.simulator import (
    Conversation,
    SimulatorConfigurationError,
)
from app.schemas.tools import FUNCTIONS, ToolError, ToolResult
from app.services import simulator as service
from app.services.tools import ToolContext, ToolDispatcher

ITEM, LINE, OPTION, QUOTE = [str(uuid4()) for _ in range(4)]


def success(**data):
    return ToolResult(ok=True, data=data)


def rejected(code):
    return ToolResult(
        ok=False,
        error=ToolError(code=code, message="Test rejection"),
    )


@pytest.fixture
def harness(monkeypatch):
    connection = MagicMock(spec=asyncpg.Connection)
    transaction = connection.transaction.return_value

    lease = MagicMock()
    lease.__aenter__ = AsyncMock(return_value=connection)
    lease.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.return_value = lease

    state = SimpleNamespace(session=Conversation(uuid4(), uuid4(), uuid4(), None, None))
    repository = SimpleNamespace(
        get_for_update=AsyncMock(side_effect=lambda _: state.session),
        pending_quote_id=AsyncMock(return_value=uuid4()),
    )

    async def save_draft(_, draft):
        state.session = replace(state.session, draft=draft)

    async def request_staff(_, reason):
        state.session = replace(
            state.session,
            staff_reason=reason,
            draft=None,
        )

    repository.set_draft = AsyncMock(side_effect=save_draft)
    repository.request_staff = AsyncMock(side_effect=request_staff)

    orders = SimpleNamespace(
        get_for_cart=AsyncMock(return_value=None),
        lock_catalog=AsyncMock(),
    )
    quotes = SimpleNamespace(clear_pending=AsyncMock())
    dispatcher = SimpleNamespace(dispatch=AsyncMock(return_value=success(version=0)))

    monkeypatch.setattr(service, "SimulatorRepository", lambda _: repository)
    monkeypatch.setattr(service, "OrderRepository", lambda _: orders)
    monkeypatch.setattr(service, "QuoteRepository", lambda _: quotes)

    factory = MagicMock(return_value=dispatcher)
    monkeypatch.setattr(service, "ToolDispatcher", factory)

    record = SimpleNamespace(model_dump=lambda **_: {"quote_id": QUOTE})
    get_quote = AsyncMock(return_value=record)
    readback = AsyncMock(return_value=SimpleNamespace(readback_token=uuid4()))
    confirm = AsyncMock()
    place = AsyncMock(return_value=record)

    monkeypatch.setattr(service, "get_quote", get_quote)
    monkeypatch.setattr(service, "complete_quote_readback", readback)
    monkeypatch.setattr(service, "confirm_quote", confirm)
    monkeypatch.setattr(service, "place_order", place)

    return SimpleNamespace(
        pool=pool,
        connection=connection,
        transaction=transaction,
        state=state,
        repository=repository,
        orders=orders,
        quotes=quotes,
        dispatcher=dispatcher,
        factory=factory,
        readback=readback,
        confirm=confirm,
        place=place,
        simulator=service.TextSimulator(pool, state.session.id),
    )


@pytest.mark.parametrize(
    "text",
    [
        "yes but remove the drink",
        "yes please",
        "confirm yes",
        f"place {QUOTE}",
        "cart other-customer",
        f"add {ITEM} 1 customer_id=other",
        "x" * 4097,
    ],
)
def test_unsupported_or_mixed_commands(text):
    with pytest.raises(service.CommandError):
        service.parse_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "menu",
        "cart",
        "quote",
        "search bowl",
        "staff Help please",
        f"add {ITEM} 2 {OPTION}",
        f"update {LINE} 1",
        f"remove {LINE}",
    ],
)
def test_tool_commands_still_match_the_schemas(text):
    name, arguments = service.parse_command(text)
    if name in service.VERSIONED:
        arguments["expected_version"] = 0

    FUNCTIONS[name][0].model_validate_json(json.dumps(arguments))


def test_yes_requires_readback_in_this_terminal(harness):
    async def scenario():
        h = harness

        assert (await h.simulator.execute("yes")).error.code == "review_required"

        offer = await h.simulator.execute("review")
        assert offer.ok

        # Returning a quote alone is not a completed readback.
        assert (await h.simulator.execute("yes")).error.code == "review_required"

        assert (await h.simulator.readback_completed(service.UUID(QUOTE))).ok

        resumed = service.TextSimulator(h.pool, h.state.session.id)
        assert (await resumed.execute("yes")).error.code == "review_required"

        h.confirm.assert_not_awaited()

        assert (await h.simulator.execute("yes")).ok
        assert h.confirm.await_args.kwargs["answer"] == "yes"
        assert h.confirm.await_args.args[0] is h.connection
        assert h.place.await_args.args[0] is h.connection
        h.orders.lock_catalog.assert_awaited_once()

        context = h.factory.call_args.args[1]
        assert context.customer_id == h.state.session.customer_id
        assert context.cart_id == h.state.session.cart_id

    asyncio.run(scenario())


def test_mixed_yes_disarms_the_previous_readback(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("review")
        await h.simulator.readback_completed(service.UUID(QUOTE))

        assert not (await h.simulator.execute("yes but make it large")).ok

        assert (await h.simulator.execute("yes")).error.code == "review_required"

        h.place.assert_not_awaited()

    asyncio.run(scenario())


def test_pending_selection_survives_resume_without_rebasing(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("review")
        await h.simulator.readback_completed(service.UUID(QUOTE))
        await h.simulator.execute("cart")

        h.dispatcher.dispatch.return_value = rejected("too_few_options")
        result = await h.simulator.execute(f"add {ITEM} 1")

        assert result.error.code == "too_few_options"
        assert (await h.simulator.execute("yes")).error.code == "selection_pending"
        assert h.state.session.draft["arguments"]["expected_version"] == 0
        h.quotes.clear_pending.assert_awaited_once()

        resumed = service.TextSimulator(h.pool, h.state.session.id)

        assert (await resumed.execute("quote")).error.code == "selection_pending"
        assert (
            await resumed.execute(f"options {OPTION}")
        ).error.code == "cart_refresh_required"

        h.dispatcher.dispatch.return_value = success(version=0)
        await resumed.execute("cart")

        h.dispatcher.dispatch.return_value = success(version=1)
        assert (await resumed.execute(f"options {OPTION}")).ok

        name, arguments = h.dispatcher.dispatch.await_args.args
        assert name == "add_cart_item"
        assert json.loads(arguments) == {
            "item_id": ITEM,
            "quantity": 1,
            "option_ids": [OPTION],
            "expected_version": 0,
        }
        assert h.state.session.draft is None

    asyncio.run(scenario())


def test_changed_cart_requires_restating_the_pending_edit(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("cart")

        h.dispatcher.dispatch.return_value = rejected("too_few_options")
        await h.simulator.execute(f"add {ITEM} 1")

        h.dispatcher.dispatch.return_value = success(version=4)
        await h.simulator.execute("cart")
        h.dispatcher.dispatch.reset_mock()

        result = await h.simulator.execute(f"options {OPTION}")

        assert result.error.code == "cart_version_conflict"
        assert h.state.session.draft is None
        h.dispatcher.dispatch.assert_not_awaited()

    asyncio.run(scenario())


def test_staff_handoff_is_checked_after_resume(harness):
    async def scenario():
        h = harness
        h.dispatcher.dispatch.return_value = success(
            status="staff_requested",
            reason="Need help",
            transfer_started=False,
        )

        assert (await h.simulator.execute("staff Need help")).ok

        resumed = service.TextSimulator(h.pool, h.state.session.id)
        assert (await resumed.execute("status")).data["status"] == "staff_requested"

        assert resumed.stopped
        assert (await resumed.execute("cart")).error.code == "staff_requested"

        h.dispatcher.dispatch.assert_awaited_once()

    asyncio.run(scenario())


def test_placement_error_escapes_the_transaction_and_consumes_receipt(
    harness,
):
    async def scenario():
        h = harness
        await h.simulator.execute("review")
        await h.simulator.readback_completed(service.UUID(QUOTE))

        h.place.side_effect = asyncpg.PostgresError("Test failure after confirmation")

        result = await h.simulator.execute("yes")
        assert result.error.code == "result_unknown"

        # The transaction sees the exception and therefore rolls back.
        assert h.transaction.__aexit__.await_args.args[0] is asyncpg.PostgresError

        assert (await h.simulator.execute("yes")).error.code == "review_required"
        assert h.place.await_count == 1

    asyncio.run(scenario())


def test_saved_order_is_recovered_without_a_new_confirmation(harness):
    async def scenario():
        h = harness
        saved = {
            "order_id": str(uuid4()),
            "quote_id": QUOTE,
        }
        h.orders.get_for_cart.return_value = SimpleNamespace(
            model_dump=lambda **_: saved
        )

        assert (await h.simulator.execute("yes")).data == saved
        h.confirm.assert_not_awaited()
        h.place.assert_not_awaited()

    asyncio.run(scenario())


def quote_data():
    return {
        "quote_id": QUOTE,
        "currency": "USD",
        "items": [
            {
                "item_name": "Bowl",
                "quantity": 2,
                "unit_price_cents": 1699,
                "line_total_cents": 3398,
                "options": [{"name": "Large"}],
            }
        ],
        "subtotal_cents": 3398,
        "tax_cents": 340,
        "total_cents": 3738,
        "pickup_at": "2026-11-13T20:30:00+00:00",
        "expires_at": "2026-11-13T20:02:00+00:00",
    }


@pytest.mark.parametrize("print_fails", [False, True])
def test_cli_prints_full_quote_before_recording_readback(
    monkeypatch,
    print_fails,
):
    events = []
    simulator = SimpleNamespace(readback_completed=AsyncMock())

    async def record(_):
        events.append("readback")
        return success(status="awaiting_confirmation")

    def output(text, **kwargs):
        events.append("print")

        if print_fails:
            raise OSError("Terminal closed")

        if len(events) == 1:
            assert kwargs["flush"]
            for expected in [
                "2 x Bowl",
                "Large",
                "$37.38 USD",
                "PST",
                "pay at pickup",
            ]:
                assert expected in text

    simulator.readback_completed.side_effect = record
    monkeypatch.setattr("builtins.print", output)

    result = success(kind="quote", quote=quote_data())

    with asyncio.Runner() as runner:
        if print_fails:
            with pytest.raises(OSError):
                cli.display_result(simulator, runner, result)
            simulator.readback_completed.assert_not_awaited()
        else:
            cli.display_result(simulator, runner, result)
            assert events[:2] == ["print", "readback"]


@pytest.mark.parametrize(
    "suffix,accepted",
    [
        ("", True),
        ("?sslmode=disable", True),
        ("?host=remote.example", False),
        ("?sslmode=disable&host=remote.example", False),
        ("?sslmode=disable&sslmode=disable", False),
        ("?sslmode=", False),
        ("?dbname=restaurant_prod", False),
        ("#fragment", False),
    ],
)
def test_local_database_query_allowlist(suffix, accepted):
    settings = Settings(
        _env_file=None,
        environment="local",
        database_url=SecretStr("postgresql://localhost/restaurant_dev" + suffix),
    )

    if accepted:
        cli.require_local_settings(settings)
    else:
        with pytest.raises(SimulatorConfigurationError):
            cli.require_local_settings(settings)


def test_nonlocal_startup_is_rejected_before_connect(monkeypatch):
    factory = AsyncMock()
    monkeypatch.setattr(cli.asyncpg, "create_pool", factory)

    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=SecretStr("postgresql://localhost/restaurant_dev"),
    )

    with pytest.raises(SimulatorConfigurationError):
        asyncio.run(cli.start_simulator(settings))

    factory.assert_not_awaited()


def test_dispatcher_uses_supplied_connection_and_propagates_database_failure():
    async def scenario():
        pool = MagicMock(spec=asyncpg.Pool)
        connection = MagicMock(spec=asyncpg.Connection)
        dispatcher = ToolDispatcher(
            pool,
            ToolContext(customer_id=uuid4(), cart_id=uuid4()),
        )
        dispatcher._execute = AsyncMock(return_value={"version": 0})

        result = await dispatcher.dispatch(
            "get_cart",
            "{}",
            connection=connection,
        )
        assert result.ok
        pool.acquire.assert_not_called()
        assert dispatcher._execute.await_args.args[0] is connection

        dispatcher._execute.side_effect = asyncpg.PostgresError("Injected DB failure")
        with pytest.raises(asyncpg.PostgresError):
            await dispatcher.dispatch(
                "get_cart",
                "{}",
                connection=connection,
            )

    asyncio.run(scenario())
