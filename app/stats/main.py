"""The stats site (st.warpflow.ru): a separate app over the same database, behind a password.

Run it next to the main app, e.g. on port 8001:
    uvicorn --factory app.stats.main:create_stats_app --port 8001
"""

import asyncio
import hmac
import time
from pathlib import Path
from typing import Annotated

import httpx
import psutil
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings
from app.db import connect, init_db
from app.stats.collect import MAX_DAYS, collect, server

PAGE = Path(__file__).with_name("page.html")
# A wrong password costs this long: guessing a long one by hand-made requests stays hopeless.
FAILED_LOGIN_DELAY = 1.0

basic = HTTPBasic(realm="WarpFlow stats")


def create_stats_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    # The first reading starts the measurement; later ones give the load since the previous.
    psutil.cpu_percent(interval=None)

    async def require_login(credentials: Annotated[HTTPBasicCredentials, Depends(basic)]) -> None:
        if not settings.stats_password:
            raise HTTPException(503, "Задайте WARPFLOW_STATS_PASSWORD в .env")
        if not hmac.compare_digest(credentials.password.encode(), settings.stats_password.encode()):
            await asyncio.sleep(FAILED_LOGIN_DELAY)
            raise HTTPException(401, "Неверный пароль", headers={"WWW-Authenticate": 'Basic realm="WarpFlow stats"'})

    app = FastAPI(title="WarpFlow stats", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(require_login)])

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/stats")
    async def stats(request: Request, days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30) -> dict:
        def gather() -> dict:
            with connect(settings.db_path) as conn:
                data = collect(conn, settings, time.time(), days)
            data["server"] = server(settings)
            return data

        data, app_status = await asyncio.gather(asyncio.to_thread(gather), _app_status(settings))
        data["app"] = app_status
        return data

    return app


async def _app_status(settings: Settings) -> dict:
    """Whether the main app answers, and what it says about its channels."""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.app_url.rstrip('/')}/api/health")
            response.raise_for_status()
            health = response.json()
    except (httpx.HTTPError, ValueError):
        return {"up": False}
    return {"up": True, "response_ms": round((time.monotonic() - started) * 1000), **health}
