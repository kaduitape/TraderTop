"""Factory da aplicacao FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.dependencies.auth import RedirectToLogin
from app.api.routes import (
    analysis,
    auth,
    dashboard,
    foto_analise,
    health,
    hedge_api,
    hedge_dashboard,
    mt5_settings,
    pulso_api,
    web_auth,
)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.maintenance.scheduler import RetentionScheduler

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Sobe e desce a manutencao periodica junto com a API.

    A retencao mora aqui porque na VPS este e o unico processo residente:
    `mt5-worker` esta atras do profile `local-mt5` e nao sobe la, e nao ha
    cron no arranjo. `RetentionScheduler.start` decide sozinho se deve
    mesmo iniciar — na suite, por exemplo, nao inicia.
    """
    agendador = RetentionScheduler()
    agendador.start()
    app.state.retention = agendador
    try:
        yield
    finally:
        agendador.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, log_dir=settings.log_dir, json_format=settings.log_json
    )

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.app_debug,
        lifespan=_lifespan,
    )

    @app.exception_handler(RedirectToLogin)
    def _redirect_to_login(_request: Request, _exc: RedirectToLogin) -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=302)

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=302)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(web_auth.router)
    app.include_router(dashboard.router)
    app.include_router(analysis.router)
    app.include_router(mt5_settings.router)
    app.include_router(foto_analise.router)
    app.include_router(pulso_api.router)
    app.include_router(hedge_api.router)
    app.include_router(hedge_dashboard.router)

    return app


app = create_app()
