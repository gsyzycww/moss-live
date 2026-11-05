"""下行播放队列的排布回归测试。

上游 MOSS-Live 实测以约 8x 实时速度下发音频（7.2s 回答 / 0.89s 送达 77 个块），
所以播放队列必然会领先实时好几秒。`player.js` 早期为了压低延迟，在队列领先
超过 1.5s 时把新块回填到 `now + START_DELAY`，与已排好的块同时出声——现象就是
"第二个回答里有两个声音叠在一起，第一个回答听起来很短"。

这里用 Node + 虚拟 AudioContext 直接喂块给真实的 player.js，断言排布永远顺序：
峰值并发 1 路、总跨度不小于音频内容长度。没有 node 时自动跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLAYER = ROOT / "app" / "static" / "js" / "player.js"
SIM = Path(__file__).resolve().parent / "js" / "queue_sim.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not PLAYER.exists(),
    reason="需要 node 才能运行播放队列仿真",
)


def run_sim(speed: float) -> dict:
    result = subprocess.run(
        ["node", str(SIM), str(PLAYER), str(speed)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"仿真失败：{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("speed", [1, 2, 8])
def test_queue_never_overlaps(speed: float) -> None:
    data = run_sim(speed)

    assert data["scheduled"] == data["expected"], "有音频块没有被排入播放队列"
    assert data["peakConcurrency"] == 1, f"{speed}x 下发时出现 {data['peakConcurrency']} 路音频同时播放"
    assert data["overlaps"] == 0, f"{speed}x 下发时出现 {data['overlaps']} 处排布重叠"


def test_queue_does_not_compress_audio() -> None:
    """8x 下发时，播放跨度必须等于音频内容长度，不能把回答压短。"""

    data = run_sim(8)
    assert data["spanSeconds"] >= data["audioSeconds"] - 0.01, (
        f"音频被压缩：内容 {data['audioSeconds']}s，实际排布只有 {data['spanSeconds']}s"
    )
