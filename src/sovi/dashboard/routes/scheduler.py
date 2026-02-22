"""Scheduler control API."""

from __future__ import annotations

from fastapi import APIRouter

from sovi.device.scheduler import get_scheduler

router = APIRouter(tags=["scheduler"])


@router.get("/api/scheduler/status")
async def scheduler_status():
    return get_scheduler().status()


@router.post("/api/scheduler/start")
async def scheduler_start():
    sched = get_scheduler()
    if sched.is_running:
        return {"ok": False, "message": "Scheduler already running"}
    sched.start()
    return {"ok": True, "message": "Scheduler started"}


@router.post("/api/scheduler/stop")
async def scheduler_stop():
    sched = get_scheduler()
    if not sched.is_running:
        return {"ok": False, "message": "Scheduler not running"}
    sched.stop()
    return {"ok": True, "message": "Scheduler stopped"}
