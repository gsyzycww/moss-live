"""应用配置。

所有配置项均可通过环境变量（或项目根目录的 .env 文件）覆盖，
变量名即字段名的大写形式，例如 `MOSS_API_KEY`、`APP_PORT`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- MOSS 平台 ----
    moss_api_key: str = Field(default="", description="MOSS API Key，仅保存在服务端")
    moss_ws_endpoint: str = Field(
        default="wss://api.mosi.cn/v1/realtime",
        description="MOSS 实时交互 WebSocket 地址",
    )
    moss_rest_endpoint: str = Field(
        default="https://api.mosi.cn", description="MOSS REST 地址（保留扩展位）"
    )
    moss_model: str = Field(default="moss-live-1.0", description="实时交互模型 ID")
    moss_tts_model: str = Field(
        default="moss-tts-1.5-flash",
        description="生成开场问候时用于合成“模拟用户语音”的 TTS 模型",
    )

    # ---- 服务 ----
    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8000)
    app_access_code: str = Field(
        default="", description="可选的访问口令；非空时浏览器需携带 ?code=xxx"
    )
    app_log_level: str = Field(default="INFO")
    app_log_events: bool = Field(
        default=False, description="是否在服务端日志中打印协议事件（排障用）"
    )
    app_test_assets: bool = Field(
        default=False, description="是否挂载 tests/fixtures 供自动化测试读取（生产请保持关闭）"
    )
    app_greeting_enabled: bool = Field(
        default=True,
        description="接通后是否播放开场问候音频（由 scripts/gen_greeting.py 预生成）",
    )
    app_greeting_delay_ms: int = Field(
        default=400, description="接通到播放问候之间的延迟（毫秒）"
    )

    # ---- 安全与限额 ----
    max_binary_frame: int = Field(default=512 * 1024, description="单帧二进制上限（字节）")
    max_session_seconds: int = Field(default=1800, description="单次通话最长时长（秒）")
    max_text_message: int = Field(default=64 * 1024, description="单条 JSON 指令上限（字节）")
    upstream_open_timeout: float = Field(default=15.0)

    @field_validator("moss_ws_endpoint")
    @classmethod
    def _check_ws_endpoint(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("MOSS_WS_ENDPOINT 必须以 ws:// 或 wss:// 开头")
        return value

    @field_validator("app_port")
    @classmethod
    def _check_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("APP_PORT 必须在 1-65535 之间")
        return value

    @property
    def api_key_configured(self) -> bool:
        return bool(self.moss_api_key.strip())

    @property
    def masked_api_key(self) -> str:
        """只保留末 4 位，避免把密钥前缀也泄露到前端。"""

        key = self.moss_api_key.strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "****"
        return f"****{key[-4:]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """清除缓存并重新读取配置（测试与热更新使用）。"""

    get_settings.cache_clear()
    return get_settings()
