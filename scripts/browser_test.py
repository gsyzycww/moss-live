#!/usr/bin/env python3
"""浏览器端到端测试（Playwright + Chrome）。

本脚本会真实打开页面、点击“开始通话”，并通过对 navigator.mediaDevices 打桩注入
一段合成语音，让模型听到真实说话内容，从而验证：

  * 落地页渲染、角色卡片、语言切换
  * 建立 WebSocket、attach_session、session.update、tagged-v1
  * AudioWorklet 采集 -> 16 kHz PCM 上行 -> 服务端转写
  * 下行 48 kHz 音频播放、播放回执、字幕与通话记录
  * 摄像头开关（canvas 合成视频流）、字幕面板、协议事件面板
  * 结开通话后的资源释放

用法：
    # 先启动带测试素材的服务
    APP_TEST_ASSETS=1 uvicorn app.main:app --port 8000
    python scripts/browser_test.py --url http://127.0.0.1:8000 --headed
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "artifacts" / "screenshots"

INIT_SCRIPT = r"""
(() => {
  const md = navigator.mediaDevices;
  if (!md) return;

  let audioPromise = null;

  async function buildAudioStream() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx({ sampleRate: 48000 });
    const res = await fetch('/test-assets/question.wav', { cache: 'no-store' });
    const raw = await res.arrayBuffer();
    const decoded = await ctx.decodeAudioData(raw);
    const src = ctx.createBufferSource();
    src.buffer = decoded;
    src.loop = true;
    const dest = ctx.createMediaStreamDestination();
    src.connect(dest);
    // 额外接一条静音支路，确保音频图持续被渲染（无物理声卡环境也能出数据）
    const mute = ctx.createGain();
    mute.gain.value = 0.0001;
    src.connect(mute);
    mute.connect(ctx.destination);
    await ctx.resume().catch(() => {});
    src.start();
    window.__mossTestAudio = { ctx, src };
    return dest.stream;
  }

  function buildVideoStream() {
    const canvas = document.createElement('canvas');
    canvas.width = 640;
    canvas.height = 480;
    const g = canvas.getContext('2d');
    setInterval(() => {
      g.fillStyle = '#0d1016';
      g.fillRect(0, 0, 640, 480);
      g.fillStyle = '#ffcf9a';
      g.font = 'bold 46px -apple-system, sans-serif';
      g.fillText('MOSS TEST', 40, 110);
      g.fillStyle = '#eceef2';
      g.font = '30px ui-monospace, monospace';
      g.fillText(new Date().toLocaleTimeString(), 40, 180);
      g.beginPath();
      g.arc(240 + Math.sin(Date.now() / 700) * 130, 330, 42, 0, Math.PI * 2);
      g.fillStyle = '#8fb6ff';
      g.fill();
    }, 80);
    return canvas.captureStream(10);
  }

  md.getUserMedia = async (constraints = {}) => {
    if (constraints && constraints.video) return buildVideoStream();
    if (!audioPromise) audioPromise = buildAudioStream();
    return audioPromise;
  };

  md.getDisplayMedia = async () => buildVideoStream();
})();
"""


async def run(url: str, headed: bool, timeout_s: int) -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    console_errors: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=not headed,
            args=[
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 960},
            permissions=["microphone", "camera"],
            locale="zh-CN",
        )
        page = await context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        await page.add_init_script(INIT_SCRIPT)

        # ---------- 落地页 ----------
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        await page.screenshot(path=str(SHOTS / "01-landing.png"))

        cards = await page.locator(".character").count()
        check("落地页渲染两个角色", cards == 2, f"检测到 {cards} 个角色卡片")

        status_state = await page.locator("#statusPill").get_attribute("data-state")
        check("服务状态正常", status_state == "ok", f"data-state={status_state}")

        title = await page.title()
        check("页面标题正确", "MOSS Live" in title, title)

        # ---------- 开始通话 ----------
        await page.locator(".call-bubble").first.click()
        await page.wait_for_selector("#call:not([hidden])", timeout=8000)
        check("通话界面已打开", True)

        await page.wait_for_function(
            "() => document.querySelector('#callStatus') && "
            "['正在聆听','思考中','正在回答','已连接'].includes(document.querySelector('#callStatus').textContent.trim())",
            timeout=25000,
        )
        status = (await page.locator("#callStatus").inner_text()).strip()
        check("会话建立并配置完成", status in {"正在聆听", "思考中", "正在回答", "已连接"}, status)

        # ---------- 语音往返 ----------
        await page.wait_for_function(
            "() => document.querySelectorAll('#transcriptBody .tp-item').length >= 2",
            timeout=timeout_s * 1000,
        )
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(SHOTS / "02-call.png"))

        items = await page.locator("#transcriptBody .tp-item p").all_inner_texts()
        user_text = " ".join(items[0::2]) if len(items) >= 2 else ""
        check("至少完成一轮问答", len(items) >= 2, f"{len(items)} 条记录")

        user_line = ""
        ai_line = ""
        nodes = await page.locator("#transcriptBody .tp-item").all()
        for node in nodes:
            cls = await node.get_attribute("class") or ""
            text = (await node.locator("p").inner_text()).strip()
            if "is-user" in cls and not user_line:
                user_line = text
            if "is-ai" in cls and len(text) > len(ai_line):
                ai_line = text
        check("用户语音被正确转写", "你好" in user_line or "介绍" in user_line, user_line[:60])
        check("模型产生语音回答文本", len(ai_line) >= 2, ai_line[:80])

        # ---------- 摄像头 ----------
        await page.locator("#camBtn").click()
        await page.wait_for_timeout(2500)
        cam_active = await page.locator("#cameraTile").get_attribute("data-active")
        check("摄像头画面已开启并推流", cam_active == "true", f"data-active={cam_active}")
        await page.screenshot(path=str(SHOTS / "03-camera.png"))

        # ---------- 字幕面板 ----------
        await page.locator("#transcriptBtn").click()
        await page.wait_for_timeout(600)
        panel_visible = await page.locator("#transcriptPanel").is_visible()
        check("字幕面板可打开", panel_visible)
        await page.screenshot(path=str(SHOTS / "04-transcript.png"))
        await page.locator("#closeTranscript").click()

        # ---------- 协议事件面板 ----------
        await page.locator("#debugBtn").click()
        await page.wait_for_timeout(600)
        rows = await page.locator("#debugBody .dbg-row").count()
        check("协议事件面板有数据", rows > 0, f"{rows} 行")
        await page.screenshot(path=str(SHOTS / "05-debug.png"))
        await page.locator("#closeDebug").click()

        # ---------- 人设面板 ----------
        await page.locator("#settingsBtn").click()
        await page.wait_for_timeout(400)
        modal_visible = await page.locator("#settingsModal").is_visible()
        check("人设面板可打开", modal_visible)
        await page.fill("#setName", "Luna测试")
        await page.locator("#savePersona").click()
        await page.wait_for_timeout(500)
        saved_name = await page.locator(".character-name").first.inner_text()
        check("人设修改已保存", saved_name.strip() == "Luna测试", saved_name)
        # 恢复默认，避免影响后续截图与手动使用
        await page.evaluate("() => localStorage.removeItem('moss.persona.overrides')")
        await page.evaluate("() => localStorage.removeItem('moss.persona.active')")
        await page.reload(wait_until="networkidle")

        # 重新进入通话，继续后面的检查
        await page.locator(".call-bubble").first.click()
        await page.wait_for_selector("#call:not([hidden])", timeout=8000)
        await page.wait_for_function(
            "() => ['正在聆听','思考中','正在回答','已连接'].includes(document.querySelector('#callStatus').textContent.trim())",
            timeout=25000,
        )

        # ---------- 静音 ----------
        await page.locator("#micBtn").click()
        pressed = await page.locator("#micBtn").get_attribute("aria-pressed")
        check("静音状态可切换", pressed == "false", f"aria-pressed={pressed}")
        await page.locator("#micBtn").click()

        # ---------- 结束通话 ----------
        await page.locator("#endBtn").click()
        await page.wait_for_timeout(1200)
        hidden = await page.locator("#call").is_hidden()
        check("通话结束后界面关闭", hidden)

        await page.screenshot(path=str(SHOTS / "06-after-call.png"))

        # ---------- 语言切换 ----------
        await page.locator("#langBtn").click()
        await page.wait_for_timeout(1200)
        en_title = await page.locator("#hero-title").inner_text()
        check("中英文切换生效", "Listen" in en_title or "phone" in en_title.lower(), en_title.replace("\n", " ")[:60])
        await page.screenshot(path=str(SHOTS / "07-english.png"))

        await browser.close()

    check("无未捕获的页面异常", not errors, "; ".join(errors[:3]))
    ignored = ("AudioContext", "favicon", "autoplay")
    real_console_errors = [e for e in console_errors if not any(k.lower() in e.lower() for k in ignored)]
    check("无前端控制台错误", not real_console_errors, "; ".join(real_console_errors[:3]))

    failed = [c for c in checks if not c[1]]
    print("\n==== 汇总 ====")
    print(f"通过 {len(checks) - len(failed)} / {len(checks)}")
    if failed:
        for name, _, detail in failed:
            print(f"  ✗ {name} {detail}")
        return 1
    print("全部通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MOSS Live 浏览器端到端测试")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--timeout", type=int, default=90, help="等待对话结果的秒数")
    args = parser.parse_args()
    return asyncio.run(run(args.url, args.headed, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
