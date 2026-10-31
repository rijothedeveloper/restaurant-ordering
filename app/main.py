from fastapi import FastAPI

from app.api.menu import router as menu_router
from app.api.router import router
from app.config import Settings, get_settings
from app.db.pool import lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db_pool = None

    app.include_router(router)
    app.include_router(menu_router)

    return app
