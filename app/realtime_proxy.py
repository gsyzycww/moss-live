"""MOSS-Live WebSocket 中继。

浏览器原生 WebSocket 无法设置 `Authorization` 请求头，API Key 也绝不能下发到前端，
因此由本服务建立上游连接并做双向转发：

    浏览器  ──►  /ws/realtime  ──►  MOSS wss://api.mosi.cn/v1/realtime
            ◄──               ◄──

中继同时承担：入站指令白名单校验、`session.update` 字段清洗、二进制帧上限控制、
可选访问口令、通话时长上限与运行指标统计。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from .config import Settings
from .protocol import (
    ACTION_ATTACH,
    ACTION_MEDIA_MODE,
    ACTION_SESSION_UPDATE,
    MEDIA_MODE_TAGGED,
    ProtocolError,
    check_binary_frame,
    validate_client_event,
)

logger = logging.getLogger("moss.relay")

_MAX_UPSTREAM_MESSAGE = 8 * 1024 * 1024


# --------------------------------------------------------------------------- 指标


@dataclass
class RelayStats:
    """进程内运行指标（供 /api/health 与日志使用）。"""

    active_sessions: int = 0
    total_sessions: int = 0
    failed_connections: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    audio_frames_up: int = 0
    video_frames_up: int = 0
    rejected_messages: int = 0
    last_error: str = ""
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_sessions": self.active_sessions,
            "total_sessions": self.total_sessions,
            "failed_connections": self.failed_connections,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "audio_frames_up": self.audio_frames_up,
            "video_frames_up": self.video_frames_up,
            "rejected_messages": self.rejected_messages,
            "last_error": self.last_error,
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }


stats = RelayStats()


def reset_stats() -> None:
    """原地重置指标（不替换对象，避免其他模块持有旧引用）。"""

    stats.active_sessions = 0
    stats.total_sessions = 0
    stats.failed_connections = 0
    stats.bytes_up = 0
    stats.bytes_down = 0
    stats.audio_frames_up = 0
    stats.video_frames_up = 0
    stats.rejected_messages = 0
    stats.last_error = ""
    stats.started_at = time.time()


# ------------------------------------------------------------------------ 工具


def build_upstream_url(settings: Settings, user_id: str | None) -> str:
    url = f"{settings.moss_ws_endpoint}?model={settings.moss_model}"
    if user_id:
        url += f"&user={user_id}"
    return url


async def send_local_error(ws: WebSocket, code: str, message: str) -> None:
    """以与上游一致的格式回传本地校验错误。"""

    try:
        await ws.send_text(
            json.dumps(
                {
                    "action": "error",
                    "data": {
                        "type": "error",
                        "error": {"code": code, "message": message},
                        "local": True,
                    },
                },
                ensure_ascii=False,
            )
        )
    except Exception:  # pragma: no cover - 连接已断开
        pass


# ------------------------------------------------------------------------ 主流程


async def run_relay(ws: WebSocket, settings: Settings, user_id: str | None = None) -> None:
    """执行一次完整的浏览器 <-> MOSS 会话中继。"""

    if not settings.api_key_configured:
        await ws.accept()
        await send_local_error(ws, "missing_api_key", "服务端未配置 MOSS_API_KEY")
        await ws.close(code=1011)
        return

    await ws.accept()
    stats.active_sessions += 1
    stats.total_sessions += 1
    session_started = time.time()
    attached = False
    closed_reason = "client_closed"
    session_bytes_up = 0
    session_bytes_down = 0

    upstream_url = build_upstream_url(settings, user_id)
    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers={"Authorization": f"Bearer {settings.moss_api_key.strip()}"},
            max_size=_MAX_UPSTREAM_MESSAGE,
            open_timeout=settings.upstream_open_timeout,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
    except InvalidStatus as exc:  # 4xx/5xx
        stats.failed_connections += 1
        stats.last_error = f"upstream_http_{exc.response.status_code}"
        logger.warning("上游握手失败：HTTP %s", exc.response.status_code)
        hint = "API Key 无效或无实时交互权限" if exc.response.status_code in (401, 403) else "上游拒绝连接"
        await send_local_error(ws, f"upstream_http_{exc.response.status_code}", hint)
        await ws.close(code=1011)
        stats.active_sessions -= 1
        return
    except (InvalidHandshake, OSError, asyncio.TimeoutError) as exc:
        stats.failed_connections += 1
        stats.last_error = f"upstream_unreachable:{type(exc).__name__}"
        logger.warning("无法连接上游：%s", exc)
        await send_local_error(ws, "upstream_unreachable", "无法连接 MOSS 实时服务，请检查网络")
        await ws.close(code=1011)
        stats.active_sessions -= 1
        return

    logger.info("会话开始 user=%s", user_id or "-")

    async def client_to_upstream() -> None:
        nonlocal attached, closed_reason, session_bytes_up
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                closed_reason = "client_closed"
                return

            text = message.get("text")
            data = message.get("bytes")

            if text is not None:
                if len(text.encode("utf-8")) > settings.max_text_message:
                    stats.rejected_messages += 1
                    await send_local_error(ws, "message_too_large", "指令过长")
                    continue
                try:
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ProtocolError("invalid_json", "指令必须是 JSON 对象")
                    event = validate_client_event(payload)
                except json.JSONDecodeError:
                    stats.rejected_messages += 1
                    await send_local_error(ws, "invalid_json", "无法解析的 JSON 指令")
                    continue
                except ProtocolError as exc:
                    stats.rejected_messages += 1
                    if settings.app_log_events:
                        logger.info("拒绝指令 %s: %s", exc.code, exc.message)
                    await send_local_error(ws, exc.code, exc.message)
                    continue

                action = event["action"]
                if action == ACTION_ATTACH:
                    if attached:
                        stats.rejected_messages += 1
                        await send_local_error(ws, "session_already_attached", "会话已加入")
                        continue
                    attached = True
                await upstream.send(json.dumps(event, ensure_ascii=False))
                continue

            if data is not None:
                try:
                    tag = check_binary_frame(data, settings.max_binary_frame)
                except ProtocolError as exc:
                    stats.rejected_messages += 1
                    await send_local_error(ws, exc.code, exc.message)
                    continue
                await upstream.send(data)
                stats.bytes_up += len(data)
                session_bytes_up += len(data)
                if tag == 0x00:
                    stats.audio_frames_up += 1
                else:
                    stats.video_frames_up += 1

    async def upstream_to_client() -> None:
        nonlocal closed_reason, session_bytes_down
        auto_media_mode = False
        async for message in upstream:
            if isinstance(message, bytes):
                stats.bytes_down += len(message)
                session_bytes_down += len(message)
                await ws.send_bytes(message)
                continue

            if settings.app_log_events:
                logger.info("<< %s", message[:300])
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await ws.send_text(message)
                continue

            action = payload.get("action")
            # 会话配置成功后确保二进制帧启用 tagged-v1 封装（幂等，浏览器也会发一次）。
            if action == "session.updated" and not payload.get("data", {}).get("error") and not auto_media_mode:
                auto_media_mode = True
                try:
                    await upstream.send(
                        json.dumps({"action": ACTION_MEDIA_MODE, "binary_protocol": MEDIA_MODE_TAGGED})
                    )
                except ConnectionClosed:
                    return
            await ws.send_text(message)

        closed_reason = "upstream_closed"

    async def watchdog() -> None:
        nonlocal closed_reason
        while True:
            await asyncio.sleep(1)
            if time.time() - session_started > settings.max_session_seconds:
                closed_reason = "session_timeout"
                await send_local_error(
                    ws, "session_timeout", "本次通话已达到服务端时长上限，已自动结束"
                )
                return

    tasks = [
        asyncio.create_task(client_to_upstream(), name="c2u"),
        asyncio.create_task(upstream_to_client(), name="u2c"),
        asyncio.create_task(watchdog(), name="watchdog"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
                logger.warning("中继任务异常：%s", exc)
                stats.last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        # 先同步完成计数与日志，避免任务被取消时跳过清理
        stats.active_sessions = max(0, stats.active_sessions - 1)
        duration = time.time() - session_started
        logger.info(
            "会话结束 reason=%s duration=%.1fs up=%dB down=%dB",
            closed_reason,
            duration,
            session_bytes_up,
            session_bytes_down,
        )
        # 注意：CancelledError 必须向上传播，Starlette 依赖它结束连接任务
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            if closed_reason == "session_timeout":
                await ws.close(code=1000)
        except Exception:
            pass
