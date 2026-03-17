"""FastAPI dashboard application — web UI + REST API."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sovi.config import settings
from sovi.db import close_pool, init_pool
from sovi.device.scheduler import get_scheduler

DASHBOARD_DIR = Path(__file__).parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    scheduler = get_scheduler()
    scheduler.guard_runtime_environment()

    stop_event = threading.Event()

    def runtime_guard_loop() -> None:
        while not stop_event.wait(settings.scheduler_runtime_guard_interval_seconds):
            with suppress(Exception):
                scheduler.guard_runtime_environment()

    monitor = None
    if settings.scheduler_runtime_guard_enabled:
        monitor = threading.Thread(
            target=runtime_guard_loop,
            name="scheduler-runtime-guard",
            daemon=True,
        )
        monitor.start()

    try:
        yield
    finally:
        stop_event.set()
        if monitor is not None:
            monitor.join(timeout=5)
        scheduler.stop()
        await close_pool()


app = FastAPI(
    title="SOVI Dashboard",
    description="Social Video Intelligence — Fleet Management & Monitoring",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Import and include route modules
from sovi.dashboard.routes import accounts, devices, events as events_routes  # noqa: E402
from sovi.dashboard.routes import overview, personas, scheduler, settings as settings_routes  # noqa: E402

app.include_router(overview.router)
app.include_router(accounts.router)
app.include_router(personas.router)
app.include_router(devices.router)
app.include_router(events_routes.router)
app.include_router(scheduler.router)
app.include_router(settings_routes.router)
