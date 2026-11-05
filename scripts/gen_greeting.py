#!/usr/bin/env python3
"""生成开场问候音频。

MOSS-Live 是「用户先开口」的全双工模型：会话配置完成后，即使一直送静音它也不会
主动说话（实测 8 秒内零下行音频）。官网的做法是让后端预先生成一段问候音频，接通
后延迟 0.4 秒本地播放。

本脚本用同样的思路，但**不引入第二个音色**：先用 TTS 合成一句「你好呀」当作模拟
用户上行，让模型用自己（也就是后续回答用的那个）声音说一句自我介绍，再把这轮下行
音频存成静态 WAV。这样问候和正片是同一个音色，不会出现「打招呼一个声音、回答另一个
声音」。

用法：
    python scripts/gen_greeting.py                 # 生成全部（角色 × 语言），已存在则跳过
    python scripts/gen_greeting.py --force         # 强制重新生成
    python scripts/gen_greeting.py --persona luna --lang zh
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.personas import PERSONAS  # noqa: E402

OUT_DIR = ROOT / "app" / "static" / "audio"
TRIGGER_TEXT = {"zh": "你好呀。", "en": "Hi there."}
GREETING_RULE = {
    "zh": "当用户只是打招呼时，用一句话自我介绍并问对方想聊什么，不超过 25 个字，不要多说别的。",
    "en": (
        "When the user only greets you, introduce yourself in one sentence and ask what "
        "they would like to talk about, under 20 words. Say nothing else."
    ),
}


def tts_pcm16k(text: str, api_key: str, model: str) -> bytes:
    """合成一句话并转成上行所需的 16 kHz 单声道 PCM S16LE。"""

    body = json.dumps({"model": model, "input": text, "response_format": "wav"}).encode()
    req = urllib.request.Request(
        f"{get_settings().moss_rest_endpoint.rstrip('/')}/v1/audio/speech",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        payload = resp.read()

    tmp = ROOT / ".greeting-trigger.wav"
    tmp.write_bytes(payload)
    try:
        with wave.open(str(tmp), "rb") as wf:
            channels, rate = wf.getnchannels(), wf.getframerate()
            data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32)
    finally:
        tmp.unlink(missing_ok=True)

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        target = int(len(data) / rate * 16000)
        data = np.interp(np.linspace(0, len(data) - 1, target), np.arange(len(data)), data)
    return data.astype("<i2").tobytes()


async def record_greeting(persona: dict, lang: str, trigger: bytes, api_key: str, model: str) -> tuple[bytes, str]:
    """把模拟用户语音送上去，收下模型这一轮的完整下行音频。"""

    url = (
        f"{get_settings().moss_ws_endpoint}?model={model}"
        f"&user=greeting-{persona['id']}"
    )
    session = {
        "persona": {"name": persona["name"], "prompt": persona["prompt"][lang]},
        "instructions": f"{persona['instructions'][lang]} {GREETING_RULE[lang]}",
    }

    audio = bytearray()
    spoken: list[str] = []
    ready = asyncio.Event()
    finished = asyncio.Event()

    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        max_size=None,
        ping_interval=20,
        open_timeout=20,
    ) as ws:
        await ws.send(json.dumps({"action": "attach_session"}))

        async def reader() -> None:
            while True:
                try:
                    message = await ws.recv()
                except Exception:
                    return
                if isinstance(message, bytes):
                    audio.extend(message)
                    continue
                payload = json.loads(message)
                action, data = payload.get("action", ""), payload.get("data") or {}
                if action == "session_attached":
                    await ws.send(json.dumps({"action": "session.update", "session": session}))
                elif action == "session.updated":
                    if data.get("error"):
                        print(f"  配置被拒绝：{data['error']}")
                        finished.set()
                        return
                    await ws.send(json.dumps({"action": "media.mode", "binary_protocol": "tagged-v1"}))
                    await ws.send(json.dumps({"action": "mic_state", "enabled": True}))
                    ready.set()
                elif action == "response.audio.context":
                    await ws.send(json.dumps({
                        "action": "playback.chunk.completed",
                        "chunk_id": data.get("chunk_id"),
                        "run_id": data.get("run_id"),
                        "turn_id": data.get("turn_id"),
                    }))
                elif action == "response.audio.done":
                    await ws.send(json.dumps({
                        "action": "playback.completed",
                        "run_id": data.get("run_id"),
                        "turn_id": data.get("turn_id"),
                    }))
                elif action == "response.audio.stop":
                    await ws.send(json.dumps({
                        "action": "playback.stopped",
                        "run_id": data.get("run_id"),
                        "turn_id": data.get("turn_id"),
                        "played_audio_ms": 0,
                    }))
                elif action == "output.transcript.done":
                    spoken.append(data.get("text", ""))
                elif action == "response.ended":
                    finished.set()
                elif action == "error":
                    print(f"  上游错误：{data.get('error')}")
                    finished.set()

        task = asyncio.create_task(reader())
        await asyncio.wait_for(ready.wait(), timeout=25)

        loop = asyncio.get_running_loop()
        # 按实时节奏上行（20 ms / 帧），说完继续送静音直到这一轮结束
        started = loop.time()
        for offset in range(0, len(trigger), 1280):
            await ws.send(b"\x00" + trigger[offset : offset + 1280])
            delay = started + (offset // 1280) * 0.02 - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

        deadline = loop.time() + 20
        silence = b"\x00" * 1280
        while not finished.is_set() and loop.time() < deadline:
            await ws.send(silence)
            await asyncio.sleep(0.02)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return bytes(audio), "".join(spoken).strip()


def trim_silence(pcm: bytes, threshold: float = 0.012, keep_ms: int = 60) -> bytes:
    """裁掉首尾静音，留一点余量，避免接通后先来一段空白。"""

    data = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if not len(data):
        return pcm
    loud = np.flatnonzero(np.abs(data) / 32768 > threshold)
    if not len(loud):
        return pcm
    pad = int(48000 * keep_ms / 1000)
    start = max(0, int(loud[0]) - pad)
    end = min(len(data), int(loud[-1]) + pad)
    return data[start:end].astype("<i2").tobytes()


def save_wav(pcm: bytes, path: Path) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.frombuffer(pcm, dtype="<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(arr.tobytes())
    return len(arr) / 48000


async def main() -> int:
    parser = argparse.ArgumentParser(description="生成 MOSS-Live 开场问候音频")
    parser.add_argument("--persona", action="append", help="角色 id，可重复；默认全部")
    parser.add_argument("--lang", action="append", choices=["zh", "en"], help="语言，可重复；默认 zh 和 en")
    parser.add_argument("--force", action="store_true", help="已存在也重新生成")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.api_key_configured:
        print("✗ 未配置 MOSS_API_KEY")
        return 1

    key = settings.moss_api_key.strip()
    personas = [p for p in PERSONAS if not args.persona or p["id"] in args.persona]
    langs = args.lang or ["zh", "en"]
    if not personas:
        print(f"✗ 没有匹配的角色：{args.persona}")
        return 1

    print(f"问候音频输出目录：{OUT_DIR.relative_to(ROOT)}")
    print(f"角色 {[p['id'] for p in personas]} · 语言 {langs}\n")

    triggers: dict[str, bytes] = {}
    for lang in langs:
        triggers[lang] = tts_pcm16k(TRIGGER_TEXT[lang], key, settings.moss_tts_model)

    failures = 0
    for persona in personas:
        for lang in langs:
            target = OUT_DIR / f"greeting-{persona['id']}-{lang}.wav"
            if target.exists() and not args.force:
                print(f"  跳过 {target.name}（已存在，--force 可重新生成）")
                continue
            print(f"  生成 {target.name} …", flush=True)
            try:
                pcm, text = await record_greeting(
                    persona, lang, triggers[lang], key, settings.moss_model
                )
            except Exception as exc:  # noqa: BLE001
                print(f"    ✗ 失败：{type(exc).__name__}: {exc}")
                failures += 1
                continue
            if not pcm:
                print("    ✗ 上游没有返回音频")
                failures += 1
                continue
            duration = save_wav(trim_silence(pcm), target)
            print(f"    ✓ {duration:.2f}s · 模型说：「{text}」")

    print()
    if failures:
        print(f"✗ {failures} 个文件生成失败")
        return 1
    print("✓ 完成。前端会在接通后自动播放对应角色的问候音频。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
