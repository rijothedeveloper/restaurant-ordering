import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr

from app.cli import simulator as cli
from app.config import Settings
from app.repositories.simulator import SimulatorConfigurationError
from app.schemas.tools import FUNCTIONS, ToolError, ToolResult
from app.services.simulator import CommandError, TextSimulator, parse_command

ITEM, LINE, OPTION, QUOTE = [str(uuid4()) for _ in range(4)]


def success(**data):
    return ToolResult(ok=True, data=data)


def rejected(code):
    return ToolResult(ok=False, error=ToolError(code=code, message="Test rejection"))


@pytest.mark.parametrize(
    "text,name,arguments",
    [
        ("menu", "get_menu", {}),
        ("cart", "get_cart", {}),
        ("quote", "create_quote", {}),
        ('search "chicken bowl"', "search_menu", {"query": "chicken bowl"}),
        ("staff Please help", "request_staff", {"reason": "Please help"}),
        (
            f"add {ITEM} 2 {OPTION}",
            "add_cart_item",
            {"item_id": ITEM, "quantity": 2, "option_ids": [OPTION]},
        ),
        (
            f"add {ITEM} 1",
            "add_cart_item",
            {"item_id": ITEM, "quantity": 1, "option_ids": []},
        ),
        (
            f"update {LINE} 3 {OPTION}",
            "update_cart_item",
            {"line_id": LINE, "quantity": 3, "option_ids": [OPTION]},
        ),
        (
            f"update {LINE} 1",
            "update_cart_item",
            {"line_id": LINE, "quantity": 1, "option_ids": []},
        ),
        (f"remove {LINE}", "remove_cart_item", {"line_id": LINE}),
        (f"place {QUOTE}", "place_order", {"quote_id": QUOTE}),
    ],
)
def test_parser_matches_existing_function_contracts(text, name, arguments):
    actual_name, actual_arguments = parse_command(text)
    assert (actual_name, actual_arguments) == (name, arguments)

    if name in {
        "add_cart_item",
        "update_cart_item",
        "remove_cart_item",
        "create_quote",
    }:
        actual_arguments["expected_version"] = 0

    # Check compatibility with the argument models from Lesson 1.
    FUNCTIONS[name][0].model_validate_json(json.dumps(actual_arguments))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "menu extra",
        "search",
        'search "unfinished',
        "quote 4",
        "confirm yes",
        "customer abc",
        "cart " + ITEM,
        "add bad 2",
        f"add {ITEM} true",
        f"add {ITEM} 2.0",
        f"add {ITEM} 1 customer_id=other",
        "remove",
        f"place {QUOTE} confirmed=true",
        "x" * 4097,
    ],
)
def test_parser_rejects_unsupported_commands(text):
    with pytest.raises(CommandError):
        parse_command(text)


@pytest.mark.parametrize(
    "code", ["cart_version_conflict", "quote_stale", "result_unknown"]
)
def test_session_requires_explicit_refresh_and_never_retries(code):
    async def scenario():
        dispatcher = SimpleNamespace(
            dispatch=AsyncMock(
                side_effect=[
                    success(version=4),
                    rejected(code),
                    success(version=6),
                    success(version=7),
                ]
            )
        )
        simulator = TextSimulator(dispatcher)

        assert (
            await simulator.execute(f"add {ITEM} 1")
        ).error.code == "cart_refresh_required"
        dispatcher.dispatch.assert_not_awaited()

        await simulator.execute("cart")
        failed = await simulator.execute(f"add {ITEM} 1")

        assert failed.error.code == code
        assert (await simulator.execute("quote")).error.code == "cart_refresh_required"
        assert dispatcher.dispatch.await_count == 2

        await simulator.execute("cart")
        assert (await simulator.execute(f"add {ITEM} 1")).ok

        calls = dispatcher.dispatch.await_args_list
        assert [call.args[0] for call in calls] == [
            "get_cart",
            "add_cart_item",
            "get_cart",
            "add_cart_item",
        ]
        assert json.loads(calls[1].args[1])["expected_version"] == 4
        assert json.loads(calls[3].args[1])["expected_version"] == 6

        for call in calls:
            assert not {"customer_id", "cart_id", "call_id"}.intersection(
                json.loads(call.args[1])
            )

    asyncio.run(scenario())


def test_success_advances_version_and_selection_rejection_preserves_it():
    async def scenario():
        dispatcher = SimpleNamespace(
            dispatch=AsyncMock(
                side_effect=[
                    success(version=0),
                    success(version=1),
                    rejected("too_few_options"),
                    success(version=2),
                    success(quote_id=QUOTE),
                ]
            )
        )
        simulator = TextSimulator(dispatcher)

        for text in [
            "cart",
            f"add {ITEM} 1",
            f"update {LINE} 2",
            f"update {LINE} 2 {OPTION}",
            "quote",
        ]:
            await simulator.execute(text)

        calls = dispatcher.dispatch.await_args_list[1:]
        assert [json.loads(call.args[1])["expected_version"] for call in calls] == [
            0,
            1,
            1,
            2,
        ]

    asyncio.run(scenario())


def test_failed_cart_read_clears_observed_version():
    async def scenario():
        dispatcher = SimpleNamespace(
            dispatch=AsyncMock(
                side_effect=[
                    success(version=4),
                    rejected("cart_not_found"),
                ]
            )
        )
        simulator = TextSimulator(dispatcher)

        await simulator.execute("cart")
        await simulator.execute("cart")
        result = await simulator.execute("quote")

        assert result.error.code == "cart_refresh_required"
        assert dispatcher.dispatch.await_count == 2

    asyncio.run(scenario())


def test_invalid_command_and_staff_stop_do_not_dispatch_more_actions():
    async def scenario():
        dispatcher = SimpleNamespace(
            dispatch=AsyncMock(return_value=success(status="staff_requested"))
        )
        simulator = TextSimulator(dispatcher)

        assert (await simulator.execute("confirm yes")).error.code == "invalid_command"
        dispatcher.dispatch.assert_not_awaited()

        assert (await simulator.execute("staff Please help")).ok
        assert simulator.stopped
        assert (await simulator.execute("cart")).error.code == "staff_requested"
        dispatcher.dispatch.assert_awaited_once()

    asyncio.run(scenario())


@pytest.fixture
def local_settings():
    return Settings(
        _env_file=None,
        environment="local",
        database_url=SecretStr(
            "postgresql://user:password@127.0.0.1:5432/restaurant_dev"
        ),
    )


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_nonlocal_environment_is_rejected_before_connect(
    local_settings, monkeypatch, environment
):
    local_settings.environment = environment
    factory = AsyncMock()
    monkeypatch.setattr(cli.asyncpg, "create_pool", factory)

    with pytest.raises(SimulatorConfigurationError, match="environment=local"):
        asyncio.run(cli.start_simulator(local_settings))

    factory.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://remote.example/restaurant_dev",
        "postgresql://localhost/restaurant_test",
        "postgresql://localhost/restaurant_prod",
        "postgresql://localhost/restaurant_dev?host=remote.example",
        "postgresql://localhost/restaurant_dev#fragment",
    ],
)
def test_unsafe_database_urls_are_rejected_before_connect(
    local_settings, monkeypatch, url
):
    local_settings.database_url = SecretStr(url)
    factory = AsyncMock()
    monkeypatch.setattr(cli.asyncpg, "create_pool", factory)

    with pytest.raises(SimulatorConfigurationError):
        asyncio.run(cli.start_simulator(local_settings))

    factory.assert_not_awaited()


@pytest.fixture
def pool_setup(monkeypatch):
    customer_id, cart_id = uuid4(), uuid4()

    connection = MagicMock(spec=asyncpg.Connection)
    connection.fetchval = AsyncMock(
        side_effect=["restaurant_dev", customer_id, cart_id]
    )

    lease = MagicMock()
    lease.__aenter__ = AsyncMock(return_value=connection)
    lease.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.return_value = lease
    pool.close = AsyncMock()

    monkeypatch.setattr(cli.asyncpg, "create_pool", AsyncMock(return_value=pool))

    return SimpleNamespace(
        pool=pool, connection=connection, customer_id=customer_id, cart_id=cart_id
    )


def test_startup_binds_database_created_ids(local_settings, pool_setup, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(cli, "ToolDispatcher", factory)

    pool, simulator = asyncio.run(cli.start_simulator(local_settings))

    context = factory.call_args.args[1]
    assert context.customer_id == pool_setup.customer_id
    assert context.cart_id == pool_setup.cart_id
    assert pool is pool_setup.pool and not simulator.stopped
    pool.close.assert_not_awaited()


def test_actual_database_mismatch_closes_pool_without_inserting(
    local_settings, pool_setup
):
    pool_setup.connection.fetchval.side_effect = ["restaurant_prod"]

    with pytest.raises(SimulatorConfigurationError, match="restaurant_dev"):
        asyncio.run(cli.start_simulator(local_settings))

    pool_setup.connection.fetchval.assert_awaited_once_with("SELECT current_database()")
    pool_setup.connection.transaction.assert_not_called()
    pool_setup.pool.close.assert_awaited_once()


@pytest.mark.parametrize("ending", ["quit", "eof", "staff"])
def test_console_help_and_exit(monkeypatch, capsys, ending):
    dispatcher = SimpleNamespace(
        dispatch=AsyncMock(
            side_effect=[
                success(version=0),
                success(status="staff_requested"),
            ]
        )
    )
    simulator = TextSimulator(dispatcher)

    commands = iter(
        ["", "help", "cart"]
        + ([] if ending == "eof" else ["staff Help" if ending == "staff" else "quit"])
    )

    def read(prompt):
        try:
            return next(commands)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read)

    with asyncio.Runner() as runner:
        cli.console(simulator, runner)

    output = capsys.readouterr().out
    assert "Commands:" in output and "Simulator ended" in output
    assert dispatcher.dispatch.await_count == (2 if ending == "staff" else 1)


def test_interrupt_closes_pool(local_settings, pool_setup, monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: local_settings)
    monkeypatch.setattr(cli, "ToolDispatcher", MagicMock())
    monkeypatch.setattr(cli, "console", MagicMock(side_effect=KeyboardInterrupt))

    cli.main([])

    pool_setup.pool.close.assert_awaited_once()