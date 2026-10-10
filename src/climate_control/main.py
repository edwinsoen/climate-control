import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from climate_control.api import router
from climate_control.config import Settings
from climate_control.db import database
from climate_control.polling import Poller
from climate_control.providers.vesync import VeSyncProvider
from climate_control.security import Vault


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)
    schema_revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    vault = Vault(settings.encryption_key)
    poller = Poller(
        sessions, settings, vault, provider or VeSyncProvider(settings.request_spacing_seconds)
    )

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(poller.run()) if settings.scheduler_enabled else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        engine.dispose()

    app = FastAPI(
        title="Climate Control",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.vault = vault
    app.state.poller = poller
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI otherwise echoes rejected password inputs in validation responses.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger(__name__).error("request_failed path=%s", request.url.path)
        return JSONResponse(
            status_code=500, content={"detail": "The request could not be completed."}
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith(("/api/", "/auth/", "/internal/")):
            response.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT id FROM accounts LIMIT 1"))
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                if revision != schema_revision:
                    raise ValueError("Migration required")
        except Exception as exc:
            raise HTTPException(503, "Database unavailable or migrations required.") from exc
        return {"status": "ready"}

    static = settings.frontend_dir
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith(("api/", "auth/", "health/", "internal/")):
            raise HTTPException(404)
        if not (static / "index.html").is_file():
            raise HTTPException(503, "Build the web UI with npm run build in frontend/.")
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return app
