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
    SavedExecution,
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
        get_execution=AsyncMock(return_value=None),
        save_execution=AsyncMock(),
        clear_readback=AsyncMock(),
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


def execution_store(h):
    saved = {}

    async def get(session_id, execution_id):
        return saved.get((session_id, execution_id))

    async def save(session_id, execution_id, request_json, result):
        saved[(session_id, execution_id)] = SavedExecution(
            request_json,
            ToolResult.model_validate_json(result.model_dump_json()),
        )

    h.repository.get_execution.side_effect = get
    h.repository.save_execution.side_effect = save
    return saved


def test_duplicate_request_returns_saved_result_and_rejects_changed_payload(
    harness,
):
    async def scenario():
        h = harness
        execution_store(h)
        await h.simulator.execute("cart")

        request = h.simulator.prepare(f"add {ITEM} 1 {OPTION}")
        h.dispatcher.dispatch.reset_mock()
        h.dispatcher.dispatch.return_value = success(version=1)

        first = await h.simulator.submit(request)
        repeated = await h.simulator.submit(request)

        assert first.ok and not first.replayed
        assert repeated.ok and repeated.replayed
        assert repeated.data == first.data
        assert repeated.execution_id == first.execution_id
        assert first.execution_id == request.execution_id
        h.dispatcher.dispatch.assert_awaited_once()

        body = json.loads(request.payload_json)
        body["arguments"]["quantity"] = 2
        changed = replace(
            request,
            payload_json=json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

        assert (await h.simulator.submit(changed)).error.code == "execution_id_conflict"

        h.dispatcher.dispatch.assert_awaited_once()

        assert (
            await h.simulator.execute("quote")
        ).error.code == "cart_refresh_required"

    asyncio.run(scenario())


def test_new_request_ids_allow_intentional_repeated_additions(harness):
    async def scenario():
        h = harness
        execution_store(h)
        await h.simulator.execute("cart")

        first = h.simulator.prepare(f"add {ITEM} 1 {OPTION}")
        h.dispatcher.dispatch.return_value = success(version=1)
        assert (await h.simulator.submit(first)).ok

        second = h.simulator.prepare(f"add {ITEM} 1 {OPTION}")
        assert second.execution_id != first.execution_id
        assert json.loads(first.payload_json)["version"] == 0
        assert json.loads(second.payload_json)["version"] == 1

        h.dispatcher.dispatch.return_value = success(version=2)
        assert (await h.simulator.submit(second)).ok

    asyncio.run(scenario())


def test_unknown_outcome_retries_the_frozen_version(harness):
    async def scenario():
        h = harness
        execution_store(h)
        await h.simulator.execute("cart")

        request = h.simulator.prepare(f"add {ITEM} 1 {OPTION}")
        h.dispatcher.dispatch.reset_mock()
        h.dispatcher.dispatch.side_effect = [
            asyncpg.PostgresError("Disconnected"),
            success(version=1),
        ]

        assert (await h.simulator.submit(request)).error.code == "result_unknown"

        assert (await h.simulator.submit(request)).ok
        assert (await h.simulator.submit(request)).replayed
        assert h.dispatcher.dispatch.await_count == 2

        for call in h.dispatcher.dispatch.await_args_list:
            assert json.loads(call.args[1])["expected_version"] == 0

    asyncio.run(scenario())


def test_prepared_edit_does_not_take_a_newer_observed_version(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("cart")

        request = h.simulator.prepare(f"update {LINE} 2 {OPTION}")

        h.dispatcher.dispatch.return_value = success(version=9)
        await h.simulator.execute("cart")

        h.dispatcher.dispatch.return_value = rejected("cart_version_conflict")
        assert (await h.simulator.submit(request)).error.code == "cart_version_conflict"

        arguments = json.loads(h.dispatcher.dispatch.await_args.args[1])
        assert arguments["expected_version"] == 0

    asyncio.run(scenario())


def test_invalid_function_arguments_are_saved_and_replayed(harness):
    async def scenario():
        h = harness
        execution_store(h)
        await h.simulator.execute("cart")

        request = h.simulator.prepare(f"add {ITEM} 0")
        h.dispatcher.dispatch.reset_mock()
        h.dispatcher.dispatch.return_value = rejected("invalid_arguments")

        first = await h.simulator.submit(request)
        again = await h.simulator.submit(request)

        assert first.error.code == "invalid_arguments"
        assert again.error.code == "invalid_arguments"
        assert again.replayed
        h.dispatcher.dispatch.assert_awaited_once()

    asyncio.run(scenario())


def test_replayed_quote_never_enables_confirmation(harness, monkeypatch):
    async def scenario():
        h = harness
        execution_store(h)
        await h.simulator.execute("cart")

        request = h.simulator.prepare("quote")
        h.dispatcher.dispatch.return_value = success(**quote_data())
        assert (await h.simulator.submit(request)).ok

        replayed = await h.simulator.replay(request.execution_id)
        assert replayed.replayed

        assert (
            await h.simulator.readback_completed(service.UUID(QUOTE))
        ).error.code == "review_required"

        assert (await h.simulator.execute("yes")).error.code == "review_required"

        return replayed

    result = asyncio.run(scenario())
    terminal = SimpleNamespace(readback_completed=AsyncMock())
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: None,
    )

    with asyncio.Runner() as runner:
        cli.display_result(terminal, runner, result)

    terminal.readback_completed.assert_not_awaited()


def test_missing_execution_and_wrong_context_do_not_dispatch(harness):
    async def scenario():
        h = harness

        assert (await h.simulator.replay(uuid4())).error.code == "execution_not_found"

        request = replace(
            h.simulator.prepare("menu"),
            session_id=uuid4(),
        )
        assert (await h.simulator.submit(request)).error.code == "context_mismatch"

        h.dispatcher.dispatch.assert_not_awaited()

    asyncio.run(scenario())


def test_options_cannot_complete_a_different_pending_draft(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("cart")
        h.dispatcher.dispatch.return_value = rejected("too_few_options")
        await h.simulator.execute(f"add {ITEM} 1")

        request = h.simulator.prepare(f"options {OPTION}")

        newer = json.loads(json.dumps(h.state.session.draft))
        newer["arguments"]["quantity"] = 2
        h.state.session = replace(h.state.session, draft=newer)

        h.dispatcher.dispatch.reset_mock()
        result = await h.simulator.submit(request)

        assert result.error.code == "selection_changed"
        assert h.state.session.draft == newer
        h.dispatcher.dispatch.assert_not_awaited()

    asyncio.run(scenario())


def test_no_remains_bound_to_the_original_quote(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("status")
        request = h.simulator.prepare("no")

        h.repository.pending_quote_id.return_value = uuid4()

        assert (await h.simulator.submit(request)).error.code == "quote_changed"

        h.quotes.clear_pending.assert_not_awaited()

    asyncio.run(scenario())


def test_mixed_confirmation_revokes_prepared_receipts(harness):
    async def scenario():
        h = harness
        await h.simulator.execute("review")
        await h.simulator.readback_completed(service.UUID(QUOTE))

        old_yes = h.simulator.prepare("yes")

        assert not (await h.simulator.execute("yes but remove something")).ok

        h.repository.clear_readback.assert_awaited_once_with(h.state.session.cart_id)

        h.confirm.side_effect = service.QuoteError(
            "quote_not_read_back",
            "Review again",
        )

        assert (await h.simulator.submit(old_yes)).error.code == "quote_not_read_back"

        h.place.assert_not_awaited()

    asyncio.run(scenario())


def test_failed_replay_does_not_claim_a_saved_result_was_returned(harness):
    async def scenario():
        h = harness
        execution_store(h)

        request = h.simulator.prepare("cart")
        assert (await h.simulator.submit(request)).ok

        h.transaction.__aexit__.side_effect = asyncpg.PostgresError("Connection lost")

        result = await h.simulator.replay(request.execution_id)
        assert result.error.code == "result_unknown"
        assert not result.replayed

    asyncio.run(scenario())
