"""开场问候音频（cue）的回归测试。

问候音频是在接通后本地播放的，不经过上游、也没有 `response.audio.context` 回执。
它必须走独立通道：一旦混进下行队列，用户开口时回答就会被排在问候后面，白等两三秒。

同样用 Node + 虚拟 AudioContext 跑真实的 `player.js`，断言：
提示音按时排期、不占用队列、下游第一块起播时间不受影响、被打断时能淡出停止。
没有 node 时自动跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLAYER = ROOT / "app" / "static" / "js" / "player.js"
SIM = Path(__file__).resolve().parent / "js" / "cue_sim.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not PLAYER.exists(),
    reason="需要 node 才能运行问候音频仿真",
)


@pytest.fixture(scope="module")
def result() -> dict:
    proc = subprocess.run(
        ["node", str(SIM), str(PLAYER)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"仿真失败：{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_cue_is_scheduled_with_delay(result: dict) -> None:
    """接通后按配置延迟起播，而不是立刻抢话。"""

    assert result["cueDelayed"], f"问候音频起播于 {result['cueScheduledAt']}s，未按 0.4s 延迟"


def test_cue_does_not_take_over_downstream_queue(result: dict) -> None:
    """问候音频不能占用下行队列的排期。"""

    assert not result["cueTookOverQueue"], "问候音频把下行队列的 nextTime 推后了"
    assert result["downstreamCount"] == 77, "有下行音频块没有排入队列"
    assert result["queueUntouched"], (
        f"下游第一块被推迟到 {result['firstDownstreamStart']}s"
        f"（应为 {result['expectedFirst']}s）"
    )


def test_cue_can_be_interrupted(result: dict) -> None:
    """用户开口或上游开始回答时，问候要能淡出停掉。"""

    assert result["stopCueReturned"], "stopCue() 没有停掉正在播放的问候"
    assert result["cueRampedToZero"], "问候音频没有淡出（gain 未 ramp 到 0）"
    assert result["cueActiveAfterStop"] is False, "停止后 cueActive 仍为 true"
