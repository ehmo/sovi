"""Device management API + HTML page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from sovi.dashboard.app import templates
from sovi.device.device_registry import (
    async_get_device,
    async_get_device_sessions,
    async_get_devices,
    async_register_device,
)

router = APIRouter(tags=["devices"])


class DeviceCreate(BaseModel):
    name: str
    udid: str
    model: str = "iPhone"
    ios_version: str = "18.3"
    wda_port: int = 8100


# --- HTML ---

@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    devices = await async_get_devices()
    return templates.TemplateResponse("devices.html", {"request": request, "devices": devices})


# --- API ---

@router.get("/api/devices")
async def list_devices():
    return await async_get_devices()


@router.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    device = await async_get_device(device_id)
    if not device:
        return {"error": "Device not found"}, 404
    return device


@router.post("/api/devices")
async def register_device(body: DeviceCreate):
    result = await async_register_device(
        name=body.name,
        udid=body.udid,
        model=body.model,
        ios_version=body.ios_version,
        wda_port=body.wda_port,
    )
    return result or {"error": "Failed to register device"}


@router.get("/api/devices/{device_id}/sessions")
async def device_sessions(device_id: str, limit: int = 20):
    return await async_get_device_sessions(device_id, limit=limit)
