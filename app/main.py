import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings
from app.db import init_db
from app.routes import avatar, download, send, upload
from app.services.cleanup import clear_tmp, run_cleanup_loop
from app.services.codes import NoFreeCodeError
from app.services.deliveries import DeliveryService
from app.services.limits import UploadTracker
from app.services.mail import MailSender
from app.services.messages import Sender
from app.services.telegram import TelegramSender


def create_app(settings: Settings | None = None, senders: dict[str, Sender] | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    if senders is None:
        senders = {"email": MailSender(settings), "telegram": TelegramSender(settings)}
    deliveries = DeliveryService(settings, senders)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        clear_tmp(settings)
        await deliveries.start()
        task = asyncio.create_task(run_cleanup_loop(settings))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await deliveries.stop()

    app = FastAPI(title="WarpFlow", lifespan=lifespan)
    app.state.settings = settings
    app.state.tracker = UploadTracker()
    app.state.deliveries = deliveries
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Retry-After"],
    )

    @app.get("/api/health")
    def health() -> dict:
        """For the stats site: the app is up, which channels work, bytes being uploaded now."""
        return {
            "channels": {name: deliveries.is_available(name) for name in senders},
            "uploading_bytes": app.state.tracker.total(),
        }

    @app.exception_handler(NoFreeCodeError)
    async def no_free_code(request: Request, exc: NoFreeCodeError) -> JSONResponse:
        return JSONResponse({"detail": "Нет свободных кодов, попробуйте позже"}, status_code=503)

    app.include_router(upload.router, prefix="/api")
    app.include_router(send.router, prefix="/api")
    app.include_router(download.router, prefix="/api")
    app.include_router(avatar.router, prefix="/api")
    return app
