import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.main import create_app
from app.repositories.restaurant_config import RestaurantConfigurationRepository
from app.services.opening_hours import is_open_now


async def main(restaurant_id: UUID, location_id: UUID) -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        async with app.state.db_pool.acquire(timeout=5) as connection:
            config = await RestaurantConfigurationRepository(connection).get(
                restaurant_id, location_id
            )

    if config is None:
        raise SystemExit("No matching restaurant and location")

    print(config.model_dump_json(indent=2))
    print("Open according to weekly hours:", is_open_now(config, datetime.now(UTC)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restaurant-id", type=UUID, required=True)
    parser.add_argument("--location-id", type=UUID, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.restaurant_id, args.location_id))
