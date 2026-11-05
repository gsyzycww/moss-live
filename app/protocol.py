"""MOSS-Live 协议常量与入站校验。

MOSS 服务端对 `session.update` 的字段校验非常严格：出现任何未定义字段会返回
`invalid_public_request` 并以 1008 关闭连接。因此浏览器发来的配置必须先在服务端
清洗，只有确定合法的字段才会转发给上游。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 客户端 -> 服务端

ACTION_ATTACH = "attach_session"
ACTION_SESSION_UPDATE = "session.update"
ACTION_MIC_STATE = "mic_state"
ACTION_MEDIA_MODE = "media.mode"
ACTION_VIDEO_STATE = "video.state"
ACTION_VIDEO_FOCUS = "video.stream_focus"
ACTION_PLAYBACK_CHUNK = "playback.chunk.completed"
ACTION_PLAYBACK_DONE = "playback.completed"
ACTION_PLAYBACK_STOPPED = "playback.stopped"

ALLOWED_CLIENT_ACTIONS: frozenset[str] = frozenset(
    {
        ACTION_ATTACH,
        ACTION_SESSION_UPDATE,
        ACTION_MIC_STATE,
        ACTION_MEDIA_MODE,
        ACTION_VIDEO_STATE,
        ACTION_VIDEO_FOCUS,
        ACTION_PLAYBACK_CHUNK,
        ACTION_PLAYBACK_DONE,
        ACTION_PLAYBACK_STOPPED,
    }
)

# ---------------------------------------------------------------- 服务端 -> 客户端

SERVER_EVENTS: dict[str, str] = {
    "session_attached": "会话已加入",
    "session.updated": "会话配置结果",
    "input.speech.started": "检测到用户开口",
    "input.speech.stopped": "检测到用户停顿",
    "input.transcript.updated": "用户转写增量",
    "input.transcript.done": "用户转写完成",
    "output.transcript.updated": "回答文本增量",
    "output.transcript.done": "回答文本完成",
    "response.audio.started": "本轮语音开始",
    "response.audio.context": "音频块上下文",
    "response.audio.done": "本轮语音结束",
    "response.audio.stop": "要求停止播放",
    "response.interrupted": "回答被打断",
    "response.ended": "本轮结束",
    "tts_speed_changed": "语速自适应调整",
    "error": "错误",
}

# 二进制 tag（media.mode=tagged-v1）
TAG_AUDIO = 0x00
TAG_CAMERA = 0x01
TAG_SCREEN = 0x02
VALID_TAGS = {TAG_AUDIO, TAG_CAMERA, TAG_SCREEN}

MEDIA_MODE_TAGGED = "tagged-v1"
VIDEO_SOURCES = {"camera", "screen"}
VIDEO_FOCUS_VALUES = {"camera", "screen", "auto"}

MAX_NAME_LEN = 64
MAX_ALIASES = 8
MAX_ALIAS_LEN = 32
MAX_PROMPT_LEN = 8000
MAX_INSTRUCTIONS_LEN = 8000


class ProtocolError(Exception):
    """入站消息不合法（不会转发给上游）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ensure_str(value: Any, field: str, max_len: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_field", f"{field} 必须是字符串")
    text = value.strip()
    if not allow_empty and not text:
        raise ProtocolError("invalid_field", f"{field} 不能为空")
    if len(text) > max_len:
        raise ProtocolError("invalid_field", f"{field} 超过 {max_len} 字符上限")
    return text


def sanitize_session(session: Any) -> dict[str, Any]:
    """清洗 `session.update` 的 session 字段，只保留上游支持的键。"""

    if not isinstance(session, dict):
        raise ProtocolError("invalid_field", "session 必须是对象")

    cleaned: dict[str, Any] = {}

    persona = session.get("persona")
    if persona is not None:
        if not isinstance(persona, dict):
            raise ProtocolError("invalid_field", "persona 必须是对象")
        persona_out: dict[str, Any] = {}
        if "name" in persona:
            persona_out["name"] = _ensure_str(persona["name"], "persona.name", MAX_NAME_LEN)
        if "prompt" in persona:
            persona_out["prompt"] = _ensure_str(
                persona["prompt"], "persona.prompt", MAX_PROMPT_LEN
            )
        if "aliases" in persona:
            aliases = persona["aliases"]
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise ProtocolError("invalid_field", "persona.aliases 必须是字符串数组")
            alias_list = [
                a.strip() for a in aliases if a and a.strip()
            ][:MAX_ALIASES]
            if any(len(a) > MAX_ALIAS_LEN for a in alias_list):
                raise ProtocolError(
                    "invalid_field", f"persona.aliases 单项不得超过 {MAX_ALIAS_LEN} 字符"
                )
            if alias_list:
                persona_out["aliases"] = alias_list
        if persona_out:
            cleaned["persona"] = persona_out

    if "instructions" in session:
        cleaned["instructions"] = _ensure_str(
            session["instructions"], "instructions", MAX_INSTRUCTIONS_LEN
        )

    if not cleaned:
        raise ProtocolError("empty_session", "session.update 未包含可识别的字段")

    return cleaned


def validate_client_event(message: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化浏览器发来的指令。返回可直接转发给上游的对象。"""

    action = message.get("action")
    if not isinstance(action, str) or action not in ALLOWED_CLIENT_ACTIONS:
        raise ProtocolError("unsupported_action", f"不支持的指令：{action!r}")

    if action == ACTION_SESSION_UPDATE:
        cleaned = dict(message)
        cleaned["session"] = sanitize_session(message.get("session"))
        return cleaned

    if action == ACTION_MIC_STATE:
        if not isinstance(message.get("enabled"), bool):
            raise ProtocolError("invalid_field", "mic_state.enabled 必须是布尔值")
        return {"action": action, "enabled": message["enabled"]}

    if action == ACTION_MEDIA_MODE:
        mode = message.get("binary_protocol")
        if mode != MEDIA_MODE_TAGGED:
            raise ProtocolError("invalid_field", "media.mode 仅支持 tagged-v1")
        return {"action": action, "binary_protocol": MEDIA_MODE_TAGGED}

    if action == ACTION_VIDEO_STATE:
        source = message.get("source")
        if source not in VIDEO_SOURCES:
            raise ProtocolError("invalid_field", "video.state.source 只能是 camera 或 screen")
        enabled = message.get("enabled")
        if not isinstance(enabled, bool):
            raise ProtocolError("invalid_field", "video.state.enabled 必须是布尔值")
        return {"action": action, "source": source, "enabled": enabled}

    if action == ACTION_VIDEO_FOCUS:
        source = message.get("source")
        if source not in VIDEO_FOCUS_VALUES:
            raise ProtocolError(
                "invalid_field", "video.stream_focus.source 只能是 camera/screen/auto"
            )
        return {"action": action, "source": source}

    if action == ACTION_PLAYBACK_CHUNK:
        chunk_id = message.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise ProtocolError("invalid_field", "playback.chunk.completed 需要 chunk_id")
        return {
            "action": action,
            "chunk_id": chunk_id,
            "run_id": message.get("run_id"),
            "turn_id": message.get("turn_id"),
        }

    if action == ACTION_PLAYBACK_DONE:
        return {
            "action": action,
            "run_id": message.get("run_id"),
            "turn_id": message.get("turn_id"),
        }

    if action == ACTION_PLAYBACK_STOPPED:
        played = message.get("played_audio_ms", 0)
        if not isinstance(played, (int, float)) or played < 0:
            raise ProtocolError("invalid_field", "played_audio_ms 必须是非负数")
        return {
            "action": action,
            "run_id": message.get("run_id"),
            "turn_id": message.get("turn_id"),
            "played_audio_ms": int(played),
        }

    if action == ACTION_ATTACH:
        return {"action": ACTION_ATTACH}

    raise ProtocolError("unsupported_action", f"不支持的指令：{action!r}")


def check_binary_frame(data: bytes, max_bytes: int) -> int:
    """校验二进制帧并返回 tag。"""

    if not data:
        raise ProtocolError("empty_frame", "空的二进制帧")
    if len(data) > max_bytes:
        raise ProtocolError(
            "frame_too_large", f"单帧超过上限 {max_bytes} 字节（收到 {len(data)}）"
        )
    tag = data[0]
    if tag not in VALID_TAGS:
        raise ProtocolError(
            "invalid_tag",
            "二进制帧缺少 tagged-v1 前缀（0x00 音频 / 0x01 摄像头 / 0x02 屏幕）",
        )
    if tag != TAG_AUDIO and len(data) < 4:
        raise ProtocolError("empty_frame", "画面帧内容为空")
    return tag
