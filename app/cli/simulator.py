import argparse
import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.repositories.simulator import (
    SimulatorConfigurationError,
    SimulatorRepository,
)
from app.schemas.tools import ToolResult
from app.services.simulator import TextSimulator

RESTAURANT_TIMEZONE = ZoneInfo("America/Los_Angeles")

HELP = """Commands:
  menu | search WORDS | cart | status
  add ITEM_UUID QUANTITY [OPTION_UUID ...]
  update LINE_UUID QUANTITY [OPTION_UUID ...]
  remove LINE_UUID
  options [OPTION_UUID ...]   Complete a saved add/update selection
  discard                    Discard the unfinished selection
  quote                      Create and read back a fresh quote
  review                     Read back the current pending quote
  yes                        Confirm the readback and place the order
  no                         Reject the pending quote
  staff REASON | help | quit

Read cart before editing, including after resume or a version conflict.
update and options replace the entire option list.
Only yes by itself confirms; make corrections with update/remove instead.
"""


def require_local_settings(settings: Settings) -> None:
    if settings.environment != "local":
        raise SimulatorConfigurationError("Simulator requires environment=local.")

    try:
        url = urlsplit(settings.database_url.get_secret_value())
        query = parse_qsl(
            url.query,
            keep_blank_values=True,
            strict_parsing=True,
        )

        allowed = (
            url.scheme in {"postgres", "postgresql"}
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
            and url.path == "/restaurant_dev"
            and url.port != 0
            and query in ([], [("sslmode", "disable")])
            and not url.fragment
        )
    except ValueError:
        allowed = False

    if not allowed:
        raise SimulatorConfigurationError(
            "Use a loopback restaurant_dev URL, optionally with ?sslmode=disable."
        )


async def close_pool(pool: asyncpg.Pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), timeout=10)
    except TimeoutError:
        pool.terminate()


async def start_simulator(
    settings: Settings,
    resume: UUID | None = None,
) -> tuple[asyncpg.Pool, TextSimulator]:
    require_local_settings(settings)

    pool = await asyncpg.create_pool(
        dsn=settings.database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        timeout=5,
        command_timeout=5,
    )

    try:
        async with pool.acquire(timeout=5) as connection:
            repository = SimulatorRepository(connection)

            if resume is None:
                session_id = await repository.create_session()
            else:
                await repository.require_session(resume)
                session_id = resume

        return pool, TextSimulator(pool, session_id)
    except BaseException:
        await close_pool(pool)
        raise


def format_quote(quote: dict[str, Any]) -> str:
    def money(cents: int) -> str:
        # Format integer cents directly without floating-point arithmetic.
        sign = "-" if cents < 0 else ""
        dollars, remainder = divmod(abs(cents), 100)
        return f"{sign}${dollars}.{remainder:02d}"

    def local_time(value: str) -> str:
        return (
            datetime.fromisoformat(value)
            .astimezone(RESTAURANT_TIMEZONE)
            .strftime("%Y-%m-%d %I:%M:%S %p %Z")
        )

    lines = [f"Quote {quote['quote_id']} — {quote['currency']}"]

    for item in quote["items"]:
        lines.append(
            f"{item['quantity']} x {item['item_name']}: "
            f"{money(item['unit_price_cents'])} each; "
            f"{money(item['line_total_cents'])} for this line"
        )
        options = ", ".join(option["name"] for option in item["options"]) or "None"
        lines.append(f"  Options: {options}")

    lines.extend(
        [
            f"Subtotal: {money(quote['subtotal_cents'])}",
            f"Tax: {money(quote['tax_cents'])}",
            f"Total: {money(quote['total_cents'])} {quote['currency']}",
            (f"ASAP pickup: {local_time(quote['pickup_at'])} (America/Los_Angeles)"),
            f"Quote expires: {local_time(quote['expires_at'])}",
            "Payment: pay at pickup.",
        ]
    )
    return "\n".join(lines)


def display_result(
    simulator: TextSimulator,
    runner: asyncio.Runner,
    result: ToolResult,
) -> None:
    if result.ok and result.data.get("kind") == "quote":
        quote = result.data["quote"]

        # A failed print must never count as a completed readback.
        print(format_quote(quote), flush=True)

        receipt = runner.run(simulator.readback_completed(UUID(quote["quote_id"])))

        if receipt.ok:
            print("Enter yes to place this order, no to reject, or make a correction.")
        else:
            print(receipt.model_dump_json(indent=2))
    else:
        print(result.model_dump_json(indent=2))


def console(
    simulator: TextSimulator,
    runner: asyncio.Runner,
) -> None:
    print(f"Simulator session: {simulator.session_id}")
    print(f"Resume: uv run python -m app.cli.simulator --resume {simulator.session_id}")
    print(HELP)

    display_result(
        simulator,
        runner,
        runner.run(simulator.execute("status")),
    )

    while not simulator.stopped:
        try:
            # No database transaction is held while waiting for input.
            text = input("restaurant> ").strip()
        except EOFError:
            break

        if not text:
            continue
        if text.casefold() == "quit":
            break
        if text.casefold() == "help":
            print(HELP)
            continue

        display_result(
            simulator,
            runner,
            runner.run(simulator.execute(text)),
        )

    print(
        "Simulator ended. Saved conversation, cart, and orders remain in the database."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local restaurant text simulator")
    parser.add_argument(
        "--resume",
        type=UUID,
        help="Resume a saved simulator session",
    )
    arguments = parser.parse_args(argv)

    try:
        with asyncio.Runner() as runner:
            pool, simulator = runner.run(
                start_simulator(get_settings(), arguments.resume)
            )
            try:
                console(simulator, runner)
            finally:
                runner.run(close_pool(pool))
    except KeyboardInterrupt:
        print("Simulator interrupted. Saved data remains in the database.")
    except SimulatorConfigurationError as error:
        raise SystemExit(str(error)) from None
    except ValidationError:
        raise SystemExit("Check the application settings in .env.") from None
    except (
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
        OSError,
        TimeoutError,
    ):
        raise SystemExit(
            "Could not run the simulator. Check the database and terminal."
        ) from None


if __name__ == "__main__":
    main()
