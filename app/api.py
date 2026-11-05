"""HTTP 接口。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query, Request

from . import __version__
from .config import Settings, get_settings
from .personas import PERSONAS, SCENARIOS, persona_payload, scenario_payload
from .realtime_proxy import stats

router = APIRouter(prefix="/api", tags=["app"])


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok" if settings.api_key_configured else "degraded",
        "version": __version__,
        "model": settings.moss_model,
        "api_key_configured": settings.api_key_configured,
        "api_key": settings.masked_api_key,
        "server_time": int(time.time()),
        "relay": stats.snapshot(),
    }


@router.get("/config")
async def config(request: Request, lang: str = Query(default="zh", pattern="^(zh|en)$")) -> dict[str, Any]:
    settings: Settings = get_settings()
    base = str(request.base_url).rstrip("/")
    return {
        "version": __version__,
        "model": settings.moss_model,
        "api_key_configured": settings.api_key_configured,
        "access_code_required": bool(settings.app_access_code),
        "language": lang,
        "personas": persona_payload(lang),  # type: ignore[arg-type]
        "scenarios": scenario_payload(lang),  # type: ignore[arg-type]
        "limits": {
            "max_session_seconds": settings.max_session_seconds,
            "max_binary_frame": settings.max_binary_frame,
            "max_persona_prompt": 8000,
            "max_instructions": 8000,
        },
        "audio": {"uplink_sample_rate": 16000, "downlink_sample_rate": 48000},
        "greeting": {
            "enabled": settings.app_greeting_enabled,
            "delay_ms": settings.app_greeting_delay_ms,
        },
        "ws_path": "/ws/realtime",
        "base_url": base,
    }


@router.get("/personas")
async def personas(lang: str = Query(default="zh", pattern="^(zh|en)$")) -> dict[str, Any]:
    return {
        "personas": persona_payload(lang),  # type: ignore[arg-type]
        "scenarios": scenario_payload(lang),  # type: ignore[arg-type]
    }


@router.get("/debug/catalog")
async def catalog() -> dict[str, Any]:
    """调试用：查看服务端内置角色与场景的原始定义。"""

    return {"personas": PERSONAS, "scenarios": SCENARIOS}
