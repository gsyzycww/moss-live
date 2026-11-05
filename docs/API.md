# 接口文档

本服务提供两类接口：**HTTP 配置接口** 与 **WebSocket 实时中继**。
中继协议与 MOSS-Live 上游一致，差别只有两点：鉴权由服务端注入；
非法指令在本地被拒绝（`data.local = true`）。交互式文档见 `/api/docs`。

Base URL：`http://127.0.0.1:8000`（部署后替换为你的域名）

---

## 1. HTTP

### 1.1 `GET /api/health`

运行状态与中继指标，用于健康检查与状态灯。

```json
{
  "status": "ok",
  "version": "1.0.0",
  "model": "moss-live-1.0",
  "api_key_configured": true,
  "api_key": "****32ff",
  "server_time": 1793634847,
  "relay": {
    "active_sessions": 1,
    "total_sessions": 12,
    "failed_connections": 0,
    "bytes_up": 1843200,
    "bytes_down": 921600,
    "audio_frames_up": 2880,
    "video_frames_up": 24,
    "rejected_messages": 0,
    "last_error": "",
    "uptime_seconds": 812.4
  }
}
```

`status`：`ok`（已配置密钥）或 `degraded`（缺少 `MOSS_API_KEY`）。

### 1.2 `GET /api/config?lang=zh|en`

前端启动时拉取的角色、场景与限额。

```json
{
  "version": "1.0.0",
  "model": "moss-live-1.0",
  "api_key_configured": true,
  "access_code_required": false,
  "language": "zh",
  "personas": [
    {
      "id": "luna",
      "name": "Luna",
      "tint": "warm",
      "tagline": "温柔、会接话的日常搭档",
      "prompt": "你是 Luna，一位温柔、活泼、有亲和力的语音助手……",
      "instructions": "始终用中文口语回答；不要输出 Markdown、列表或表情符号。"
    }
  ],
  "scenarios": [
    { "id": "chat", "name": "自由对话", "icon": "chat", "instructions": "" },
    { "id": "interpreter", "name": "同声传译", "icon": "translate", "instructions": "……" }
  ],
  "limits": {
    "max_session_seconds": 1800,
    "max_binary_frame": 524288,
    "max_persona_prompt": 8000,
    "max_instructions": 8000
  },
  "audio": { "uplink_sample_rate": 16000, "downlink_sample_rate": 48000 },
  "greeting": { "enabled": true, "delay_ms": 400 },
  "ws_path": "/ws/realtime",
  "base_url": "http://127.0.0.1:8000"
}
```

`greeting` 控制接通后的开场问候：前端按 `enabled` 决定是否播放，`delay_ms` 为接通到
播放之间的延迟。音频文件位于 `/static/audio/greeting-<角色>-<语言>.wav`，
由 `scripts/gen_greeting.py` 预生成；文件不存在时前端静默跳过。

非法 `lang` 返回 422。

### 1.3 `GET /api/personas?lang=zh|en`

`/api/config` 的子集，只含 `personas` 与 `scenarios`。

### 1.4 `GET /api/debug/catalog`

返回服务端内置角色/场景的**原始定义**（含双语），仅用于调试。

### 1.5 `GET /api/docs` · `GET /api/openapi.json`

FastAPI 自带的 Swagger UI 与 OpenAPI 描述。

---

## 2. WebSocket `/ws/realtime`

### 2.1 连接

```
ws://127.0.0.1:8000/ws/realtime?user=<可选ID>&code=<可选口令>
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user` | 否 | 稳定终端用户 ID，`[A-Za-z0-9_.:-]{1,64}`，非法值会被忽略 |
| `code` | 否 | 服务端设置 `APP_ACCESS_CODE` 时必填 |

握手无需任何请求头（鉴权在服务端完成）。连接建立后先发 `attach_session`。

### 2.2 客户端 → 服务端

所有消息为 JSON 文本，`action` 为指令名。**不在下表内的指令会被本地拒绝**。

| action | 字段 | 说明 |
| --- | --- | --- |
| `attach_session` | — | 加入会话；每条连接只能发一次 |
| `session.update` | `session.persona{name,aliases,prompt}`、`session.instructions` | 配置人设与规则；未知字段会被剔除 |
| `mic_state` | `enabled: bool` | 开关麦克风输入 |
| `media.mode` | `binary_protocol: "tagged-v1"` | 启用二进制封装（服务端也会自动补发） |
| `video.state` | `source: "camera"\|"screen"`, `enabled: bool` | 开关某路画面 |
| `video.stream_focus` | `source: "camera"\|"screen"\|"auto"` | 两路同时开启时的关注来源 |
| `playback.chunk.completed` | `chunk_id`, `run_id`, `turn_id` | 单个音频块**实际播放完成** |
| `playback.completed` | `run_id`, `turn_id` | 本轮全部音频播放完成 |
| `playback.stopped` | `run_id`, `turn_id`, `played_audio_ms` | 被打断后回传已播时长 |

示例：

```json
{"action":"session.update","session":{
  "persona":{"name":"Luna","aliases":["小月"],"prompt":"你是温柔的女声助手 Luna。"},
  "instructions":"用两三句话回答，不要用 Markdown。"
}}
```

```json
{"action":"video.state","source":"screen","enabled":true}
```

### 2.3 二进制帧（`tagged-v1`）

一条 WebSocket 复用三路媒体，每帧首字节是标签：

| tag | 内容 | 要求 |
| --- | --- | --- |
| `0x00` | 音频 | 16 kHz、单声道、PCM S16LE，20 ms/帧，按实时节奏发送 |
| `0x01` | 摄像头 | 单帧 JPEG，建议 ≤ 640 px 长边、1 FPS |
| `0x02` | 屏幕 | 单帧 JPEG，建议 ≤ 1024 px 长边、1 FPS |

限制：单帧 ≤ `MAX_BINARY_FRAME`（默认 512 KB）；缺标签或空帧会被拒绝。

```js
const frame = new Uint8Array(1 + pcm16.byteLength);
frame[0] = 0x00;
frame.set(new Uint8Array(pcm16.buffer), 1);
ws.send(frame);
```

### 2.4 服务端 → 客户端

| action | data 主要字段 | 说明 |
| --- | --- | --- |
| `session_attached` | `session_id`, `memory_scope`, `delegation` | 已加入会话，开始计费 |
| `session.updated` | `error`, `tools` | `error` 为空表示配置生效 |
| `input.speech.started` | — | 检测到用户开口 |
| `input.speech.stopped` | — | 检测到停顿 |
| `input.transcript.updated` | `text`（全量）, `turn` | 用户转写增量，**替换**而非追加 |
| `input.transcript.done` | `text`, `turn` | 用户转写定稿 |
| `output.transcript.updated` | `text`（全量）, `run_id`, `turn_id` | 回答文本增量 |
| `output.transcript.done` | `text`, `run_id`, `turn_id` | 回答文本定稿 |
| `response.audio.started` | `kind`, `run_id`, `turn_id` | 本轮语音开始 |
| `response.audio.context` | `chunk_id`, `run_id`, `turn_id` | 紧随其后的二进制音频属于该块 |
| `response.audio.done` | `run_id`, `turn_id` | 本轮音频发送完毕 |
| `response.audio.stop` | `reason`, `run_id`, `turn_id` | 要求立即停播（`reason` 常见为 `barge-in`） |
| `response.interrupted` | `reason`, `text`, `run_id` | 回答被打断，`text` 为已说出的内容 |
| `response.ended` | `status`, `run_id`, `turn_id` | `completed` / `aborted` / `failed` |
| `tts_speed_changed` | `speed` | 模型自适应语速，仅提示 |
| `error` | `type`, `error{code,message}`, `local?` | `local:true` 表示由本服务拒绝，连接仍可用 |

二进制下行：48 kHz、单声道、PCM S16LE，**无标签字节**，与 `response.audio.context`
严格 1:1 交替。

### 2.5 错误码

| code | 来源 | 处理建议 |
| --- | --- | --- |
| `access_denied` | 本地 | 检查 URL 的 `code` |
| `missing_api_key` | 本地 | 服务端配置 `MOSS_API_KEY` |
| `unsupported_action` | 本地 | 指令名拼写错误或未开放 |
| `invalid_field` / `empty_session` | 本地 | 按错误信息修字段；连接不受影响 |
| `invalid_tag` / `frame_too_large` / `empty_frame` | 本地 | 检查二进制帧封装 |
| `session_already_attached` | 本地 | `attach_session` 只能发一次 |
| `upstream_http_401/403` | 本地 | Key 无效或没有 MOSS-Live 权限 |
| `upstream_unreachable` | 本地 | 网络不通/代理问题 |
| `session_timeout` | 本地 | 达到 `MAX_SESSION_SECONDS`，需重连 |
| `connection_closed` | 前端生成 | WebSocket 异常断开 |
| `invalid_public_request` | 上游 | 会话字段非法（服务端已清洗，正常不应出现） |

### 2.6 最小可用流程

1. 连接 → `{"action":"attach_session"}`
2. 收到 `session_attached` → 发 `session.update`
3. 收到 `session.updated{error:""}` → 发 `media.mode`、`mic_state{true}`
4. 持续发 `0x00 + PCM16k`
5. 收到 `response.audio.context` → 等下一帧二进制 → 播放 → 回 `playback.chunk.completed`
6. 收到 `response.audio.done` 且播放完毕 → 回 `playback.completed`
7. 收到 `response.audio.stop` → 立即停播 → 回 `playback.stopped{played_audio_ms}`
8. 关闭 WebSocket 结束（停止计费）

### 2.7 Python 客户端示例

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8000/ws/realtime?user=e2e") as ws:
        await ws.send(json.dumps({"action": "attach_session"}))
        async for raw in ws:
            if isinstance(raw, bytes):
                continue                      # 48 kHz PCM
            msg = json.loads(raw)
            if msg["action"] == "session_attached":
                await ws.send(json.dumps({"action": "session.update", "session": {
                    "persona": {"name": "Luna"}, "instructions": "一句话回答"}}))
            elif msg["action"] == "session.updated" and not msg["data"]["error"]:
                await ws.send(json.dumps({"action": "media.mode", "binary_protocol": "tagged-v1"}))
                await ws.send(json.dumps({"action": "mic_state", "enabled": True}))
            elif msg["action"] == "response.audio.context":
                d = msg["data"]
                await ws.send(json.dumps({"action": "playback.chunk.completed",
                                          "chunk_id": d["chunk_id"],
                                          "run_id": d["run_id"], "turn_id": d["turn_id"]}))

asyncio.run(main())
```

完整可运行版本见 `scripts/e2e_live.py`（含音频上行、断言与结果保存）。
