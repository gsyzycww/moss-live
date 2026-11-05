# 测试报告

> 最近一次完整执行：2026-11-03 · macOS (darwin) · Python 3.13.12 · Chrome (Playwright) ·
> 上游 `moss-live-1.0`（真实 API Key）

## 1. 结论

| 测试层 | 用例/断言数 | 结果 | 备注 |
| --- | --- | --- | --- |
| 单元测试（协议） | 22 | ✅ 全部通过 | 字段清洗、白名单、二进制帧 |
| 接口测试（HTTP） | 9 | ✅ 全部通过 | health/config/personas/静态资源/问候音频 |
| 中继测试（假上游） | 6 | ✅ 全部通过 | 鉴权注入、转发、回执、配额 |
| 播放队列仿真（Node） | 4 | ✅ 全部通过 | 1x/2x/8x 下发下排布不重叠、不压缩 |
| 问候音频仿真（Node） | 3 | ✅ 全部通过 | 按时延迟起播、不占用队列、可被打断 |
| 真实 API 端到端 | 9 | ✅ 全部通过 | 经中继完成一次真实语音问答 |
| 浏览器端到端（Playwright） | 18 | ✅ 全部通过 | 含真实语音往返、摄像头、面板、i18n |
| **合计** | **71** | **✅ 0 失败** | `pytest` 44 项 + 脚本 27 项断言 |

```
$ pytest -q
44 passed, 1 warning in 1.61s

$ python scripts/e2e_live.py
PASS 收到 session_attached / 会话配置成功 / 用户转写 / 模型回答文本
PASS 收到下行音频（4.80s）/ 音频块回执（50 个）/ 播放完成回执 / completed / 无错误
结果：全部通过

$ python scripts/browser_test.py
通过 18 / 18 · 全部通过
```

---

## 2. 测试环境

| 项目 | 值 |
| --- | --- |
| 操作系统 | macOS (darwin)，会话内无物理声卡/摄像头 |
| Python | 3.13.12（项目 venv） |
| FastAPI / uvicorn | 0.162.0 / 0.54.0 |
| websockets | 17.4 |
| 浏览器 | Google Chrome（Playwright `channel="chrome"`，headless） |
| 上游 | `wss://api.mosi.cn/v1/realtime`，模型 `moss-live-1.0` |
| 测试音频 | `tests/fixtures/question.wav`（7.12 s，48 kHz 单声道，含 4 s 静音） |

---

## 3. 分层说明

### 3.1 单元测试 `tests/test_protocol.py`（22 项）

覆盖“上游会断线”的所有边界：

- `sanitize_session`：保留合法字段、剔除 `voice_id/voice/memory_scope/temperature`
  等未知字段、去除空白、别名数量与长度上限、prompt 8000 字上限、
  空 session 拒绝、类型错误拒绝。
- `validate_client_event`：9 种允许的指令全部通过；未知 action、缺 action、
  `media.mode` 非 `tagged-v1`、`video.state` 非法 source/类型、
  `playback.*` 缺字段或负数 → 精确错误码。
- `check_binary_frame`：三个合法 tag、空帧、未知 tag、超限、空画面帧。

**修复的真实 bug**：早期版本会把前端传来的 `voice_id` 原样转发，导致上游
`invalid_public_request` 直接断线；现在被 `sanitize_session` 拦截。

### 3.2 接口测试 `tests/test_api.py`（9 项）

- `/api/health` 状态、模型、指标结构；**密钥只回显 `****后四位`**。
- `/api/config` 中英文、角色顺序、场景数量、音频采样率、非法语言 422。
- `/api/personas` 结构；`/` 首页含标题且 `Cache-Control: no-store`。
- 5 个静态资源 200；OpenAPI 可访问。

### 3.3 中继测试 `tests/test_relay.py`（6 项）

用本地 `websockets` 假上游（`FakeUpstream`）断言下列行为（6 个用例覆盖）：

1. 上游收到的 `Authorization: Bearer test-key-123456`（密钥注入正确）。
2. 上游 URL 带 `model` 与 `user` 查询参数。
3. `session.update` 中的 `voice_id` 被剥离，`session.updated` 正常回传。
4. 服务端自动补发 `media.mode: tagged-v1`。
5. 二进制音频帧被转发，下游音频/事件顺序正确（`context → binary → done → ended`）。
6. `playback.chunk.completed` 被转发给上游。
7. 未知指令、非法二进制帧在本地被拒绝，**连接不断**，可继续使用。
8. 访问口令：无 `code` 被拒；带正确 `code` 可连接。
9. `/api/health` 的 `active_sessions` 在连接结束后归零。

**修复的真实 bug**：中继 `finally` 中先 `await` 再减计数，任务被取消时
`CancelledError` 会在第一个 `await` 抛出，导致 `active_sessions` 永久泄漏；
现在先同步减计数再做异步清理（`test_health_counts_sessions` 回归保护）。

### 3.4 真实 API 端到端 `scripts/e2e_live.py`（9 项）

脚本模拟浏览器经本地中继与上游通信：

- 用 numpy 把 WAV 重采样到 16 kHz PCM S16LE，按 20 ms/帧、实时节奏上行；
- 说完继续送静音，直到本轮结束；
- 自动回 `playback.chunk.completed` / `playback.completed`。

断言结果（本次）：

```
用户转写：请你用一句话介绍一下你自己。
模型回答：你好，我是小安，能帮你查信息、聊问题，也能陪你闲谈。
下行音频：460800 bytes = 4.80s
音频块回执：50 个，本轮 completed
```

同时验证了真实协议细节：音频块与 `response.audio.context` 严格 1:1；
`response.ended.status == completed`。

### 3.5 浏览器端到端 `scripts/browser_test.py`（18 项）

真实打开页面并点击通话，覆盖：

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | 落地页渲染两个角色 | ✅ |
| 2 | 服务状态灯 `data-state=ok` | ✅ |
| 3 | 页面标题正确 | ✅ |
| 4 | 点击电话打开通话层 | ✅ |
| 5 | 会话建立并配置完成（状态「正在聆听」） | ✅ |
| 6 | 至少完成一轮问答（记录 ≥ 2 条） | ✅ |
| 7 | 用户语音被正确转写 | ✅「请你用一句话介绍一下你自己。」 |
| 8 | 模型产生回答文本 | ✅「嗨，我是Luna，你的语音小伙伴…」 |
| 9 | 摄像头开启并推流（画面 tile 激活） | ✅ |
| 10 | 字幕面板可打开 | ✅ |
| 11 | 协议事件面板有数据 | ✅ 180+ 行 |
| 12 | 人设面板可打开 | ✅ |
| 13 | 人设修改保存后角色名更新 | ✅「Luna测试」 |
| 14 | 静音状态可切换 | ✅ `aria-pressed=false` |
| 15 | 结束通话后界面关闭 | ✅ |
| 16 | 中英文切换生效 | ✅「Listen and speak together.」 |
| 17 | 无未捕获页面异常 | ✅ |
| 18 | 无前端控制台错误 | ✅ |

**关于媒体注入**：本会话环境没有物理音频设备，`getUserMedia` 会挂起。
测试通过 `page.add_init_script` 覆写 `navigator.mediaDevices`，
用 `AudioBufferSourceNode → MediaStreamAudioDestinationNode` 构造一段**真实语音**
的 MediaStream（内容即 `tests/fixtures/question.wav`），并用 canvas
`captureStream()` 模拟摄像头。这样仍然完整走了
`AudioWorklet → 16 kHz PCM → 中继 → 上游 → 转写 → 回答` 的真实链路，
只是“设备”换成了合成源。

---

## 4. 人工验收清单（真实设备，建议你在自己电脑上过一遍）

自动化测试不能替代真实麦克风/摄像头，请按下面 6 步确认：

1. 打开 <http://127.0.0.1:8000>，右上角显示「服务正常」。
2. 点 Luna 下方电话 → 浏览器询问麦克风 → 允许 → 状态变为「正在聆听」。
3. 说「你好，给我讲个笑话」→ 1 秒内看到你的字幕、听到回答、光球随声音律动。
4. 在它说话时插话「等等」→ 语音立刻停止，记录里该条显示「被打断」。
5. 开摄像头对准一本书，问「画面里有什么」→ 能描述画面内容；
   打开屏幕共享后「关注画面」按钮出现，可切换 camera/screen/auto。
6. 点结束 → 出现「通话结束 · 00:xx，共 N 轮对话」提示；
   展开「字幕」→「导出」得到一份 Markdown 记录。

---

## 5. 复现方式

```bash
cd /Users/xxx/moss-live
source .venv/bin/activate

# 1) 单元 + 接口 + 中继（不消耗 API 额度）
pytest tests -q

# 2) 真实 API（需要 .env 中的 Key，消耗少量额度）
python scripts/e2e_live.py
python scripts/e2e_live.py --verbose      # 打印全部协议事件
python scripts/e2e_live.py --direct       # 直连上游，便于对比中继

# 3) 浏览器端到端（需要测试素材挂载）
APP_TEST_ASSETS=1 uvicorn app.main:app --port 8001 &
python scripts/browser_test.py --url http://127.0.0.1:8001
python scripts/browser_test.py --headed   # 可视化观察
```

产物：`artifacts/screenshots/*.png`（7 张）、`artifacts/e2e-response.wav`（模型回答音频，
可用 `ffplay`/QuickTime 试听，也可再用 `moss-transcribe-1.0` 转写验证内容）。

---

## 6. 测试中发现并修复的问题

| 编号 | 问题 | 影响 | 修复 |
| --- | --- | --- | --- |
| 1 | `session.update` 原样转发未知字段 | 上游 1008 断线，用户侧表现为“莫名掉线” | `sanitize_session` 白名单清洗 + 单测 |
| 2 | 中继 `finally` 中先 `await` 后减计数 | `active_sessions` 泄漏，指标失真 | 先同步清理；补 `test_health_counts_sessions` |
| 3 | 中继清理用 `except BaseException` | 吞掉 `CancelledError`，TestClient/连接任务无法结束 | 改为只捕获 `Exception`，让取消语义透传 |
| 4 | `getUserMedia` 无响应时页面永远卡在「正在连接」 | 无声卡/权限卡住时无法使用 | 采集加 10 s 超时，失败降级为只听模式并提示 |
| 5 | 修改角色名后卡片不更新 | 人设编辑“看起来没生效” | `renderStage` 读取本地覆盖值 |
| 6 | `/api/health` 暴露密钥前 6 位 | 信息泄露风险 | 只回显 `****末四位` |
| 7 | 移动端顶栏按钮文字换行 | 小屏布局错乱 | ≤720 px 只保留图标 + `nowrap` |

---

## 7. 已知限制与未覆盖项

- 未做真实硬件（物理麦克风/摄像头）自动化：受执行环境限制，见 §3.5。
- 未做弱网/丢包模拟：协议本身有静音补发与 512 KB 缓冲上限，但未量化丢包率影响。
- 未覆盖 Safari：`getDisplayMedia` 与 AudioWorklet 行为差异较大，建议 Chrome/Edge。
- 未做长时间稳定性（>30 min）压测：`MAX_SESSION_SECONDS` 默认 1800 s，
  上游本身按在线时长计费，长通话建议自建监控。
- 上游不支持中途改人设与工具调用，相关路径未实现（已在文档标注）。
