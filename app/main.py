"""FastAPI 应用入口。

启动方式：
    uvicorn app.main:app --host 127.0.0.1 --port 8000
或：
    python -m app.main
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .config import STATIC_DIR, get_settings
from .realtime_proxy import run_relay

log = logging.getLogger("moss")

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.app_log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    app = FastAPI(
        title="MOSS Live Realtime Demo",
        version=__version__,
        description="复刻 MOSS-Live 官方实时语音 Demo 的本地部署版本",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # 仅在显式开启时挂载测试素材（浏览器自动化测试需要读取一段语音）
    if settings.app_test_assets:
        fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
        if fixtures.is_dir():
            app.mount("/test-assets", StaticFiles(directory=fixtures), name="test-assets")

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/audio/"):
            # 开场问候音频是生成好的静态资产，不会随代码改动变化，允许短暂缓存，
            # 免得每次刷新页面都重新拉一遍几百 KB。
            response.headers["Cache-Control"] = "public, max-age=3600"
        elif path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "img" / "favicon.svg", media_type="image/svg+xml")

    @app.websocket("/ws/realtime")
    async def realtime_socket(websocket: WebSocket) -> None:
        if settings.app_access_code:
            code = websocket.query_params.get("code", "")
            if code != settings.app_access_code:
                await websocket.accept()
                await websocket.send_text(
                    '{"action":"error","data":{"type":"error","error":'
                    '{"code":"access_denied","message":"访问口令不正确"},"local":true}}'
                )
                await websocket.close(code=1008)
                return

        raw_user = websocket.query_params.get("user", "")
        user_id = raw_user if USER_ID_RE.match(raw_user) else None

        try:
            await run_relay(websocket, settings, user_id)
        except WebSocketDisconnect:
            log.info("浏览器主动断开")
        except Exception:  # pragma: no cover - 兜底保护
            log.exception("中继异常")
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        log.exception("未处理异常：%s", exc)
        return JSONResponse({"error": "internal_error", "message": str(exc)}, status_code=500)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    cfg = get_settings()
    if not cfg.api_key_configured:
        print("[warn] 未检测到 MOSS_API_KEY，请检查 .env 或环境变量", file=sys.stderr)
    uvicorn.run(
        "app.main:app",
        host=cfg.app_host,
        port=cfg.app_port,
        reload=False,
        log_level=cfg.app_log_level.lower(),
    )
