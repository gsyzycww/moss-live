"""HTTP 接口测试。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] in {"ok", "degraded"}
        assert data["model"] == "moss-live-1.0"
        assert "relay" in data
        # 只暴露末 4 位，绝不下发完整密钥
        assert data["api_key"].startswith("****")
        assert len(data["api_key"]) <= 8


def test_config_zh():
    with TestClient(app) as client:
        res = client.get("/api/config?lang=zh")
        assert res.status_code == 200
        data = res.json()
        assert [p["id"] for p in data["personas"]] == ["luna", "leo"]
        assert len(data["scenarios"]) >= 4
        assert data["audio"]["uplink_sample_rate"] == 16000
        assert data["audio"]["downlink_sample_rate"] == 48000
        assert data["ws_path"] == "/ws/realtime"
        assert data["greeting"]["enabled"] is True
        assert data["greeting"]["delay_ms"] == 400


def test_config_en():
    with TestClient(app) as client:
        res = client.get("/api/config?lang=en")
        assert res.status_code == 200
        data = res.json()
        assert data["personas"][0]["tagline"].startswith("A warm")


def test_config_rejects_bad_lang():
    with TestClient(app) as client:
        assert client.get("/api/config?lang=fr").status_code == 422


def test_personas_endpoint():
    with TestClient(app) as client:
        res = client.get("/api/personas")
        data = res.json()
        assert {"personas", "scenarios"} <= set(data)
        assert data["personas"][1]["name"] == "Leo"


def test_index_page():
    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "MOSS Live" in res.text
        assert res.headers["cache-control"].startswith("no-store")


def test_static_assets():
    with TestClient(app) as client:
        for path in [
            "/static/css/app.css",
            "/static/js/app.js",
            "/static/js/session.js",
            "/static/worklets/capture-processor.js",
            "/favicon.svg",
        ]:
            assert client.get(path).status_code == 200, path


def test_openapi_docs():
    with TestClient(app) as client:
        assert client.get("/api/openapi.json").status_code == 200


def test_greeting_audio_served():
    """预生成的开场问候音频应可直接访问，并允许短暂缓存。"""

    audio = Path(__file__).resolve().parent.parent / "app" / "static" / "audio"
    files = sorted(audio.glob("greeting-*.wav"))
    if not files:
        pytest.skip("尚未生成问候音频（运行 scripts/gen_greeting.py）")

    with TestClient(app) as client:
        for item in files:
            res = client.get(f"/static/audio/{item.name}")
            assert res.status_code == 200, item.name
            assert res.content[:4] == b"RIFF", f"{item.name} 不是合法 WAV"
            assert res.headers["cache-control"].startswith("public")
