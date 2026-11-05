"""协议校验单元测试：入站指令清洗与二进制帧校验。"""

import pytest

from app.protocol import (
    ProtocolError,
    check_binary_frame,
    sanitize_session,
    validate_client_event,
)


class TestSanitizeSession:
    def test_keeps_supported_fields(self):
        session = sanitize_session(
            {
                "persona": {"name": "Luna", "aliases": ["小月"], "prompt": "你是 Luna"},
                "instructions": "用一句话回答",
            }
        )
        assert session == {
            "persona": {"name": "Luna", "aliases": ["小月"], "prompt": "你是 Luna"},
            "instructions": "用一句话回答",
        }

    def test_drops_unknown_fields(self):
        """上游对未知字段会直接断开连接，必须在这里剔除。"""

        session = sanitize_session(
            {
                "persona": {"name": "Luna", "voice_id": "xxx", "temperature": 0.9},
                "instructions": "hi",
                "voice": "abc",
                "memory_scope": "user",
            }
        )
        assert session == {"persona": {"name": "Luna"}, "instructions": "hi"}

    def test_strips_whitespace(self):
        session = sanitize_session({"persona": {"name": "  Luna  "}})
        assert session["persona"]["name"] == "Luna"

    def test_limits_alias_count_and_length(self):
        session = sanitize_session(
            {"persona": {"name": "L", "aliases": [f"a{i}" for i in range(20)]}}
        )
        assert len(session["persona"]["aliases"]) == 8
        with pytest.raises(ProtocolError):
            sanitize_session({"persona": {"name": "L", "aliases": ["x" * 40]}})

    def test_rejects_too_long_prompt(self):
        with pytest.raises(ProtocolError) as err:
            sanitize_session({"persona": {"name": "L", "prompt": "x" * 8001}})
        assert err.value.code == "invalid_field"

    def test_rejects_empty_session(self):
        with pytest.raises(ProtocolError) as err:
            sanitize_session({"voice_id": "abc"})
        assert err.value.code == "empty_session"

    def test_rejects_non_string_name(self):
        with pytest.raises(ProtocolError):
            sanitize_session({"persona": {"name": 123}})

    def test_rejects_bad_alias_type(self):
        with pytest.raises(ProtocolError):
            sanitize_session({"persona": {"name": "L", "aliases": "abc"}})


class TestValidateClientEvent:
    def test_allows_known_actions(self):
        event = validate_client_event({"action": "mic_state", "enabled": True})
        assert event == {"action": "mic_state", "enabled": True}

    def test_rejects_unknown_action(self):
        with pytest.raises(ProtocolError) as err:
            validate_client_event({"action": "response.create"})
        assert err.value.code == "unsupported_action"

    def test_rejects_missing_action(self):
        with pytest.raises(ProtocolError):
            validate_client_event({})

    def test_media_mode_only_tagged(self):
        event = validate_client_event({"action": "media.mode", "binary_protocol": "tagged-v1"})
        assert event["binary_protocol"] == "tagged-v1"
        with pytest.raises(ProtocolError):
            validate_client_event({"action": "media.mode", "binary_protocol": "raw"})

    def test_video_state_validation(self):
        assert validate_client_event({"action": "video.state", "source": "screen", "enabled": True})
        with pytest.raises(ProtocolError):
            validate_client_event({"action": "video.state", "source": "webcam", "enabled": True})
        with pytest.raises(ProtocolError):
            validate_client_event({"action": "video.state", "source": "camera", "enabled": "yes"})

    def test_video_focus_validation(self):
        assert validate_client_event({"action": "video.stream_focus", "source": "auto"})
        with pytest.raises(ProtocolError):
            validate_client_event({"action": "video.stream_focus", "source": "mic"})

    def test_playback_chunk_requires_id(self):
        event = validate_client_event(
            {"action": "playback.chunk.completed", "chunk_id": "fa-1", "run_id": "r1", "turn_id": "t1"}
        )
        assert event["chunk_id"] == "fa-1"
        with pytest.raises(ProtocolError):
            validate_client_event({"action": "playback.chunk.completed"})

    def test_playback_stopped_requires_non_negative(self):
        event = validate_client_event(
            {"action": "playback.stopped", "run_id": "r", "turn_id": "t", "played_audio_ms": 1200}
        )
        assert event["played_audio_ms"] == 1200
        with pytest.raises(ProtocolError):
            validate_client_event(
                {"action": "playback.stopped", "run_id": "r", "turn_id": "t", "played_audio_ms": -5}
            )

    def test_session_update_is_sanitized(self):
        event = validate_client_event(
            {"action": "session.update", "session": {"persona": {"name": "L"}, "bogus": 1}}
        )
        assert event["session"] == {"persona": {"name": "L"}}


class TestBinaryFrame:
    def test_accepts_all_three_tags(self):
        assert check_binary_frame(b"\x00" + b"\x00" * 10, 1024) == 0
        assert check_binary_frame(b"\x01" + b"\xff" * 10, 1024) == 1
        assert check_binary_frame(b"\x02" + b"\xff" * 10, 1024) == 2

    def test_rejects_empty(self):
        with pytest.raises(ProtocolError):
            check_binary_frame(b"", 1024)

    def test_rejects_unknown_tag(self):
        with pytest.raises(ProtocolError) as err:
            check_binary_frame(b"\x09" + b"\x00" * 10, 1024)
        assert err.value.code == "invalid_tag"

    def test_rejects_oversized(self):
        with pytest.raises(ProtocolError) as err:
            check_binary_frame(b"\x00" + b"\x00" * 2048, 1024)
        assert err.value.code == "frame_too_large"

    def test_rejects_empty_video_payload(self):
        with pytest.raises(ProtocolError):
            check_binary_frame(b"\x01", 1024)
