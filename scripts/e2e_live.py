#!/usr/bin/env python3
"""真实 API 端到端测试：经本地中继完成一次完整语音问答。

链路：脚本（模拟浏览器）→ 本地中继 /ws/realtime → MOSS-Live 上游。
脚本会把一段 16 kHz PCM 语音按实时节奏上行，随后校验转写、回答、下行音频与回执。

用法：
    python scripts/e2e_live.py                       # 经本地中继
    python scripts/e2e_live.py --direct              # 直连上游（对比用）
    python scripts/e2e_live.py --audio my.wav        # 自定义提问音频
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import wave
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "question.wav"
ARTIFACTS = ROOT / "artifacts"

PERSONA = {
    "persona": {
        "name": "小安",
        "prompt": "你是友好的助手小安，语气温和、简洁，像是在打电话。",
    },
    "instructions": "请用一句简短的中文回答，不要超过 30 个字。",
}


def load_pcm16k(path: Path) -> bytes:
    """读取 WAV 并重采样到 16 kHz 单声道 PCM S16LE。"""

    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if width != 2:
        raise SystemExit(f"只支持 16-bit PCM WAV，当前 {width * 8}-bit")

    data = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1).astype("<i2")

    if rate != 16000:
        duration = len(data) / rate
        target_len = int(duration * 16000)
        src_idx = np.linspace(0, len(data) - 1, target_len)
        data = np.interp(src_idx, np.arange(len(data)), data.astype(np.float32)).astype("<i2")

    return data.tobytes()


async def run(url: str, audio: Path, timeout: float, verbose: bool, headers: dict | None = None) -> int:
    pcm = load_pcm16k(audio)
    duration = len(pcm) / 2 / 16000
    print(f"提问音频：{audio.name} · {duration:.2f}s · {len(pcm)} bytes PCM16k")

    events: list[tuple[str, dict]] = []
    audio_out = bytearray()
    transcripts: dict[str, str] = {}
    errors: list[str] = []
    pending_context: list[dict] = []
    receipts = {"chunk": 0, "completed": 0, "stopped": 0}

    async with websockets.connect(
        url, additional_headers=headers or {}, max_size=None, ping_interval=20
    ) as ws:
        await ws.send(json.dumps({"action": "attach_session"}))
        ready = asyncio.Event()
        finished = asyncio.Event()

        async def reader() -> None:
            while True:
                try:
                    message = await ws.recv()
                except Exception:
                    return
                if isinstance(message, bytes):
                    if len(audio_out) == 0 and pending_context:
                        print(f"首个音频块：{len(message)} bytes（{len(message) / 2 / 48000 * 1000:.0f} ms）")
                    audio_out.extend(message)
                    continue

                payload = json.loads(message)
                action = payload.get("action", "")
                data = payload.get("data") or {}
                events.append((action, data))
                if verbose and action not in {
                    "output.transcript.updated",
                    "input.transcript.updated",
                    "response.audio.context",
                }:
                    print(f"  << {action} {json.dumps(data, ensure_ascii=False)[:160]}")

                if action == "session_attached":
                    await ws.send(json.dumps({"action": "session.update", "session": PERSONA}))
                elif action == "session.updated":
                    if data.get("error"):
                        errors.append(f"session.updated: {data['error']}")
                        finished.set()
                        return
                    await ws.send(json.dumps({"action": "media.mode", "binary_protocol": "tagged-v1"}))
                    await ws.send(json.dumps({"action": "mic_state", "enabled": True}))
                    ready.set()
                elif action == "response.audio.context":
                    pending_context.append(data)
                    await ws.send(json.dumps({
                        "action": "playback.chunk.completed",
                        "chunk_id": data.get("chunk_id"),
                        "run_id": data.get("run_id"),
                        "turn_id": data.get("turn_id"),
                    }))
                    receipts["chunk"] += 1
                elif action == "response.audio.done":
                    await ws.send(json.dumps({
                        "action": "playback.completed",
                        "run_id": data.get("run_id"),
                        "turn_id": data.get("turn_id"),
                    }))
                    receipts["completed"] += 1
                elif action == "response.audio.stop":
                    await ws.send(json.dumps({
                        "action": "playback.stopped",
                        "run_id": data.get("run_id"),
                        "turn_id": data.get("turn_id"),
                        "played_audio_ms": 0,
                    }))
                    receipts["stopped"] += 1
                elif action == "input.transcript.done":
                    transcripts["user"] = data.get("text", "")
                elif action == "output.transcript.done":
                    transcripts["ai"] = data.get("text", "")
                elif action == "response.ended":
                    if data.get("status") != "completed":
                        errors.append(f"response.ended status={data.get('status')}")
                    finished.set()
                elif action == "error":
                    err = data.get("error") or {}
                    errors.append(f"{err.get('code')}: {err.get('message')}")

        task = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            print("✗ 会话未能在 20s 内完成配置")
            task.cancel()
            return 1

        print("会话已就绪，开始按实时节奏上行 16 kHz PCM …")
        chunk = 640 * 2  # 20 ms
        started = asyncio.get_running_loop().time()
        for offset in range(0, len(pcm), chunk):
            await ws.send(b"\x00" + pcm[offset : offset + chunk])
            target = started + (offset // chunk) * 0.02
            delay = target - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)

        # 说完后继续送静音，直到本轮结束（最多 timeout 秒）
        silence = b"\x00" * chunk
        deadline = asyncio.get_running_loop().time() + timeout
        while not finished.is_set() and asyncio.get_running_loop().time() < deadline:
            await ws.send(b"\x00" + silence)
            await asyncio.sleep(0.02)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ---------------------------------------------------------------- 断言
    actions = [action for action, _ in events]
    ok = True

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"{'PASS' if condition else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
        if not condition:
            ok = False

    check("收到 session_attached", "session_attached" in actions)
    check("会话配置成功", "session.updated" in actions and not any("session.updated:" in e for e in errors))
    check("收到用户转写", bool(transcripts.get("user")), transcripts.get("user", ""))
    check("收到模型回答文本", bool(transcripts.get("ai")), transcripts.get("ai", ""))
    check("收到下行音频", len(audio_out) > 0, f"{len(audio_out)} bytes = {len(audio_out) / 2 / 48000:.2f}s")
    check("音频块回执已发送", receipts["chunk"] > 0, f"{receipts['chunk']} 个块")
    check("本轮播放完成回执", receipts["completed"] > 0)
    check("本轮以 completed 结束", "response.ended" in actions and not any("status=" in e for e in errors))
    check("没有错误事件", not errors, "; ".join(errors[:3]))

    if audio_out:
        if not ARTIFACTS.exists():
            ARTIFACTS.mkdir(parents=True)
        out = ARTIFACTS / "e2e-response.wav"
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(bytes(audio_out))
        print(f"回答音频已保存：{out}")

    summary = {}
    for action, _ in events:
        summary[action] = summary.get(action, 0) + 1
    print("\n事件统计：", json.dumps(summary, ensure_ascii=False))
    print(f"上行 {duration:.2f}s 语音 · 下行 {len(audio_out) / 2 / 48000:.2f}s 音频")
    print("\n结果：", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="MOSS Live 真实 API 端到端测试")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/realtime", help="本地中继地址")
    parser.add_argument("--direct", action="store_true", help="绕过中继，直连上游")
    parser.add_argument("--api-key", default=os.environ.get("MOSS_API_KEY", ""))
    parser.add_argument("--audio", type=Path, default=FIXTURE)
    parser.add_argument("--timeout", type=float, default=40.0, help="等待本轮结束的秒数")
    parser.add_argument("--verbose", action="store_true", help="打印全部协议事件")
    args = parser.parse_args()

    if not args.audio.exists():
        print(f"找不到提问音频：{args.audio}", file=sys.stderr)
        return 2

    if args.direct:
        if not args.api_key:
            print("直连模式需要 --api-key 或环境变量 MOSS_API_KEY", file=sys.stderr)
            return 2
        print("直连模式：跳过中继，直接连接 MOSS-Live")
        return asyncio.run(
            run(
                "wss://api.mosi.cn/v1/realtime?model=moss-live-1.0&user=e2e-direct",
                args.audio,
                args.timeout,
                args.verbose,
                headers={"Authorization": f"Bearer {args.api_key}"},
            )
        )

    return asyncio.run(run(args.url, args.audio, args.timeout, args.verbose))


if __name__ == "__main__":
    sys.exit(main())
