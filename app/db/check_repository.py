import asyncio
import os

from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.repositories.restaurants import RestaurantRepository


async def main() -> None:
    test_url = os.getenv("TEST_DATABASE_URL")
    if not test_url:
        raise RuntimeError("Load TEST_DATABASE_URL from .env.dbmate")

    settings = Settings(
        _env_file=None,
        environment="test",
        debug=False,
        database_url=SecretStr(test_url),
        db_pool_min_size=1,
        db_pool_max_size=2,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.db_pool.acquire(timeout=5) as connection:
            database = await connection.fetchval("SELECT current_database()")
            if database != "restaurant_test":
                raise RuntimeError("This check requires restaurant_test")

            print("Connected to restaurant_test")
            repository = RestaurantRepository(connection)
            transaction = connection.transaction()
            await transaction.start()

            try:
                created = await repository.create("Rijo's Demo Kitchen")
                loaded = await repository.get_by_id(created.id)

                if loaded != created:
                    raise RuntimeError("The saved restaurant did not match")
                print("PASS: inserted and read a restaurant")
            finally:
                await transaction.rollback()

            if await repository.get_by_id(created.id) is not None:
                raise RuntimeError("The verification row was not rolled back")

            print("PASS: verification row rolled back")

    print("PASS: application pool closed")


if __name__ == "__main__":
    asyncio.run(main())