import argparse
import asyncio
from urllib.parse import urlsplit

import asyncpg
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.repositories.simulator import SimulatorConfigurationError, SimulatorRepository
from app.services.simulator import TextSimulator
from app.services.tools import ToolContext, ToolDispatcher

HELP = """Commands:
  menu
  search WORDS
  cart
  add ITEM_UUID QUANTITY [OPTION_UUID ...]
  update LINE_UUID QUANTITY [OPTION_UUID ...]
  remove LINE_UUID
  quote
  place QUOTE_UUID
  staff REASON
  help
  quit

Run cart before the first edit. Read cart again after a version conflict.
update replaces all selected options; omit option IDs to select none.
Confirmation is connected in Lesson 3; place rejects unconfirmed quotes.
"""


def require_local_settings(settings: Settings) -> None:
    if settings.environment != "local":
        raise SimulatorConfigurationError("Simulator requires environment=local.")

    try:
        url = urlsplit(settings.database_url.get_secret_value())
        allowed = (
            url.scheme in {"postgres", "postgresql"}
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
            and url.path == "/restaurant_dev"
            and not url.query
            and not url.fragment
        )
    except ValueError:
        allowed = False

    if not allowed:
        raise SimulatorConfigurationError(
            "Simulator requires a loopback restaurant_dev URL without query parameters."
        )


async def close_pool(pool: asyncpg.Pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), timeout=10)
    except TimeoutError:
        pool.terminate()


async def start_simulator(settings: Settings) -> tuple[asyncpg.Pool, TextSimulator]:
    # Reject non-local settings before opening a database connection.
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
            customer_id, cart_id = await SimulatorRepository(
                connection
            ).create_context()

        context = ToolContext(customer_id=customer_id, cart_id=cart_id)
        simulator = TextSimulator(ToolDispatcher(pool, context))
        return pool, simulator

    except BaseException:
        await close_pool(pool)
        raise


def console(simulator: TextSimulator, runner: asyncio.Runner) -> None:
    print(f"Simulator session: {simulator.session_id}")
    print("Created a new development customer and cart. Type cart to begin.")
    print(HELP)

    while not simulator.stopped:
        try:
            # input() runs outside the event loop, between completed operations.
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

        result = runner.run(simulator.execute(text))
        print(result.model_dump_json(indent=2))

    print("Simulator ended. Saved carts and orders remain in the database.")


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description="Local restaurant text simulator").parse_args(
        argv
    )

    try:
        settings = get_settings()

        # Keep one event loop for the pool and dispatcher throughout this run.
        with asyncio.Runner() as runner:
            pool, simulator = runner.run(start_simulator(settings))
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

    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        raise SystemExit(
            "Could not use the development database. Check PostgreSQL and migrations."
        ) from None


if __name__ == "__main__":
    main()