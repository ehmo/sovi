"""FastAPI dashboard application — web UI + REST API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sovi.db import close_pool, init_pool

DASHBOARD_DIR = Path(__file__).parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
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
from sovi.dashboard.routes import overview, scheduler, settings  # noqa: E402

app.include_router(overview.router)
app.include_router(accounts.router)
app.include_router(devices.router)
app.include_router(events_routes.router)
app.include_router(scheduler.router)
app.include_router(settings.router)
