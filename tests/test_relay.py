"""中继行为测试：使用本地假上游，验证鉴权注入、字段清洗、二进制转发与回执。"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve


class FakeUpstream:
    """最小化的 MOSS-Live 上游，只实现本测试关心的交互。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []
        self.authorization: str | None = None
        self.path: str | None = None
        self.port: int | None = None
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(5), "假上游启动失败"

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        async def handler(ws) -> None:
            self.authorization = ws.request.headers.get("Authorization")
            self.path = ws.request.path
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        self.messages.append(("bin", raw))
                        for event in (
                            {"action": "response.audio.context",
                             "data": {"chunk_id": "c1", "run_id": "r1", "turn_id": "t1"}},
                        ):
                            await ws.send(json.dumps(event))
                        await ws.send(b"\x10\x20" * 240)
                        await ws.send(json.dumps(
                            {"action": "response.audio.done", "data": {"run_id": "r1", "turn_id": "t1"}}))
                        await ws.send(json.dumps(
                            {"action": "response.ended",
                             "data": {"status": "completed", "run_id": "r1", "turn_id": "t1"}}))
                    else:
                        payload = json.loads(raw)
                        self.messages.append(("json", payload))
                        action = payload.get("action")
                        if action == "attach_session":
                            await ws.send(json.dumps({
                                "action": "session_attached",
                                "data": {"session_id": "fake-session", "memory_scope": "session", "delegation": "none"},
                            }))
                        elif action == "session.update":
                            await ws.send(json.dumps({
                                "action": "session.updated", "data": {"error": "", "tools": []},
                            }))
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()


@pytest.fixture()
def upstream():
    server = FakeUpstream()
    server.start()
    yield server
    server.stop()


def build_app(monkeypatch, upstream: FakeUpstream, **env):
    values = {
        "MOSS_WS_ENDPOINT": f"ws://127.0.0.1:{upstream.port}/v1/realtime",
        "MOSS_API_KEY": "test-key-123456",
        "APP_ACCESS_CODE": "",
    }
    values.update(env)
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    from app.config import get_settings
    from app.realtime_proxy import reset_stats

    get_settings.cache_clear()
    reset_stats()
    from app.main import create_app

    return create_app()


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_relay_injects_auth_and_forwards_events(monkeypatch, upstream):
    app = build_app(monkeypatch, upstream)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/realtime?user=unit-test") as ws:
            ws.send_text(json.dumps({"action": "attach_session"}))
            attached = json.loads(ws.receive_text())
            assert attached["action"] == "session_attached"
            assert attached["data"]["session_id"] == "fake-session"

            ws.send_text(json.dumps({
                "action": "session.update",
                "session": {"persona": {"name": "Luna", "voice_id": "not-allowed"}, "instructions": "hi"},
            }))
            updated = json.loads(ws.receive_text())
            assert updated["action"] == "session.updated"
            assert updated["data"]["error"] == ""

            assert upstream.authorization == "Bearer test-key-123456"
            assert upstream.path.startswith("/v1/realtime?model=moss-live-1.0")
            assert "user=unit-test" in upstream.path

            assert wait_for(lambda: any(
                kind == "json" and payload.get("action") == "session.update"
                for kind, payload in upstream.messages
            ))
            sent = next(p for kind, p in upstream.messages if kind == "json" and p.get("action") == "session.update")
            assert sent["session"] == {"persona": {"name": "Luna"}, "instructions": "hi"}

            # 会话配置成功后，中继会自动补发 tagged-v1，保证二进制帧可被解析
            assert wait_for(lambda: any(
                kind == "json" and p.get("action") == "media.mode" for kind, p in upstream.messages
            ))


def test_relay_forwards_audio_and_playback_receipts(monkeypatch, upstream):
    app = build_app(monkeypatch, upstream)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/realtime") as ws:
            ws.send_text(json.dumps({"action": "attach_session"}))
            ws.receive_text()
            ws.send_text(json.dumps({"action": "session.update", "session": {"persona": {"name": "Luna"}}}))
            ws.receive_text()

            ws.send_bytes(b"\x00" + b"\x00\x01" * 320)

            context = json.loads(ws.receive_text())
            assert context["action"] == "response.audio.context"
            audio = ws.receive()
            assert len(audio.get("bytes") or b"") == 480

            assert json.loads(ws.receive_text())["action"] == "response.audio.done"
            assert json.loads(ws.receive_text())["action"] == "response.ended"

            ws.send_text(json.dumps({
                "action": "playback.chunk.completed",
                "chunk_id": "c1", "run_id": "r1", "turn_id": "t1",
            }))
            assert wait_for(lambda: any(
                kind == "json" and p.get("action") == "playback.chunk.completed"
                and p.get("chunk_id") == "c1"
                for kind, p in upstream.messages
            ))


def test_relay_rejects_unknown_action_locally(monkeypatch, upstream):
    app = build_app(monkeypatch, upstream)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/realtime") as ws:
            ws.send_text(json.dumps({"action": "attach_session"}))
            ws.receive_text()
            ws.send_text(json.dumps({"action": "session.create"}))
            error = json.loads(ws.receive_text())
            assert error["action"] == "error"
            assert error["data"]["local"] is True
            assert error["data"]["error"]["code"] == "unsupported_action"

            ws.send_text(json.dumps({"action": "session.update", "session": {"voice": "x"}}))
            error = json.loads(ws.receive_text())
            assert error["data"]["error"]["code"] == "empty_session"

            # 连接未被上游拒绝，可以继续使用
            ws.send_text(json.dumps({"action": "mic_state", "enabled": True}))
            assert wait_for(lambda: any(
                kind == "json" and p.get("action") == "mic_state" for kind, p in upstream.messages
            ))


def test_relay_rejects_invalid_binary(monkeypatch, upstream):
    app = build_app(monkeypatch, upstream)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/realtime") as ws:
            ws.send_text(json.dumps({"action": "attach_session"}))
            ws.receive_text()
            ws.send_bytes(b"\x09garbage")
            error = json.loads(ws.receive_text())
            assert error["data"]["error"]["code"] == "invalid_tag"

            ws.send_bytes(b"\x00" + b"\x00" * (600 * 1024))
            error = json.loads(ws.receive_text())
            assert error["data"]["error"]["code"] == "frame_too_large"


def test_relay_access_code(monkeypatch, upstream):
    app = build_app(monkeypatch, upstream, APP_ACCESS_CODE="s3cret")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/realtime") as ws:
            error = json.loads(ws.receive_text())
            assert error["data"]["error"]["code"] == "access_denied"

        with client.websocket_connect("/ws/realtime?code=s3cret") as ws:
            ws.send_text(json.dumps({"action": "attach_session"}))
            assert json.loads(ws.receive_text())["action"] == "session_attached"


def test_health_counts_sessions(monkeypatch, upstream):
    app = build_app(monkeypatch, upstream)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/realtime") as ws:
            ws.send_text(json.dumps({"action": "attach_session"}))
            ws.receive_text()
            time.sleep(0.1)
            data = client.get("/api/health").json()
            assert data["relay"]["total_sessions"] >= 1
            assert data["relay"]["active_sessions"] >= 1
        time.sleep(0.3)
        data = client.get("/api/health").json()
        assert data["relay"]["active_sessions"] == 0
