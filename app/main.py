import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import firebase_admin
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.menu import router as menu_router
from app.api.router import router
from app.api.staff import router as staff_router
from app.config import Settings, get_settings
from app.db.pool import lifespan as database_lifespan


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
        raise RuntimeError("Firebase Auth emulator is not enabled for this application")

    settings: Settings = app.state.settings
    firebase_app = None

    if settings.firebase_project_id is not None:
        firebase_app = firebase_admin.initialize_app(
            options={
                "projectId": settings.firebase_project_id,
                "httpTimeout": 5,
            },
            name=f"restaurant-{uuid4().hex}",
        )

    app.state.firebase_app = firebase_app

    try:
        async with database_lifespan(app):
            yield
    finally:
        app.state.firebase_app = None
        if firebase_app is not None:
            firebase_admin.delete_app(firebase_app)


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
    app.state.firebase_app = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(settings.staff_web_origin).rstrip("/")],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(router)
    app.include_router(menu_router)
    app.include_router(staff_router)

    return app
