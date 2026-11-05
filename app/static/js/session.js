/**
 * MOSS-Live 实时会话客户端。
 *
 * 负责：连接本地中继 -> attach_session -> session.update -> 开启媒体 ->
 * 上行音频/画面、下行事件与音频、播放回执、打断处理、错误与状态机。
 */

import { AudioPlayer } from './player.js';

export const SessionState = {
  IDLE: 'idle',
  CONNECTING: 'connecting',
  ATTACHED: 'attached',
  READY: 'ready',
  LIVE: 'live',
  CLOSED: 'closed',
  FAILED: 'failed',
};

const ATTACH_TIMEOUT_MS = 10000;
const MAX_BUFFERED_BYTES = 512 * 1024;

export class RealtimeSession {
  constructor(options) {
    this.url = options.url;
    this.persona = options.persona || { name: 'Luna' };
    this.instructions = options.instructions || '';
    this.onEvent = options.onEvent || (() => {});
    this.onState = options.onState || (() => {});
    this.onError = options.onError || (() => {});
    this.onLevel = options.onLevel || (() => {});

    this.ws = null;
    this.state = SessionState.IDLE;
    this.sessionId = '';
    this.pendingInfo = null;
    this.runs = new Map();
    this.intentionalClose = false;
    this.droppedAudioFrames = 0;
    this.sentAudioFrames = 0;
    this.sentVideoFrames = 0;
    this.metrics = { lastResponseMs: null, firstAudioAt: null };
    this._attachTimer = null;
    this._lastSpeechEndAt = null;

    this.player = new AudioPlayer({
      onChunkDone: (item) => this._handleChunkDone(item),
      onDrain: () => this._handleDrain(),
      onLevel: this.onLevel,
      onCueDone: (info) => this._emit('ui.cue_done', info),
    });
  }

  /** 播放本地问候音频（不经过上游，也不产生回执）。 */
  playCue(buffer, delaySeconds) {
    return this.player.playCue(buffer, delaySeconds);
  }

  /** 打断本地问候音频；上游音频不受影响。 */
  stopCue(fadeSeconds) {
    return this.player.stopCue(fadeSeconds);
  }

  // ------------------------------------------------------------ 生命周期

  connect() {
    if (this.ws) return;
    this._setState(SessionState.CONNECTING);
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (err) {
      this._fail('ws_construct_failed', `无法建立连接：${err.message}`);
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    this._attachTimer = setTimeout(() => {
      if (this.state === SessionState.CONNECTING) {
        this._fail('attach_timeout', '连接超时：服务端没有回应，请检查本地服务与网络');
      }
    }, ATTACH_TIMEOUT_MS);

    ws.onopen = () => {
      this._sendJson({ action: 'attach_session' });
      this._emit('client.connected', {});
    };

    ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        this._handleText(event.data);
      } else {
        this._handleBinary(event.data);
      }
    };

    ws.onerror = () => {
      this._emit('client.socket_error', {});
    };

    ws.onclose = (event) => {
      clearTimeout(this._attachTimer);
      const wasIntentional = this.intentionalClose || this.state === SessionState.FAILED;
      this._setState(SessionState.CLOSED);
      this._emit('client.closed', { code: event.code, reason: event.reason });
      if (!wasIntentional) {
        this.onError({
          code: 'connection_closed',
          message: event.code === 1006 ? '连接意外断开（网络或服务不可用）' : `连接已关闭（${event.code}）`,
          fatal: true,
        });
      }
    };
  }

  async close() {
    this.intentionalClose = true;
    clearTimeout(this._attachTimer);
    this.runs.clear();
    this.pendingInfo = null;
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) {
      try { this.ws.close(1000, 'client_end'); } catch (_) { /* noop */ }
    }
    this.ws = null;
    await this.player.close();
    this._setState(SessionState.CLOSED);
  }

  // ------------------------------------------------------------ 上行

  _sendJson(payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(payload));
    this._emit('client.send', payload);
    return true;
  }

  sendAudio(int16Frame) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (this.state !== SessionState.READY && this.state !== SessionState.LIVE) return;
    if (this.ws.bufferedAmount > MAX_BUFFERED_BYTES) {
      this.droppedAudioFrames += 1;
      return;
    }
    const bytes = new Uint8Array(1 + int16Frame.byteLength);
    bytes[0] = 0x00;
    bytes.set(new Uint8Array(int16Frame.buffer, int16Frame.byteOffset, int16Frame.byteLength), 1);
    this.ws.send(bytes);
    this.sentAudioFrames += 1;
  }

  sendVideo(source, payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (this.state !== SessionState.READY && this.state !== SessionState.LIVE) return;
    const tag = source === 'screen' ? 0x02 : 0x01;
    const bytes = new Uint8Array(1 + payload.byteLength);
    bytes[0] = tag;
    bytes.set(payload, 1);
    this.ws.send(bytes);
    this.sentVideoFrames += 1;
  }

  setMicEnabled(enabled) {
    this._sendJson({ action: 'mic_state', enabled: Boolean(enabled) });
  }

  setVideoState(source, enabled) {
    this._sendJson({ action: 'video.state', source, enabled: Boolean(enabled) });
  }

  setFocus(source) {
    this._sendJson({ action: 'video.stream_focus', source });
  }

  // ------------------------------------------------------------ 下行

  _handleText(raw) {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch (_) {
      return;
    }
    const action = msg.action;
    const data = msg.data || {};
    this._emit(action, data);

    switch (action) {
      case 'session_attached': {
        clearTimeout(this._attachTimer);
        this.sessionId = data.session_id || '';
        this._setState(SessionState.ATTACHED);
        const session = { persona: {} };
        if (this.persona.name) session.persona.name = this.persona.name;
        if (this.persona.prompt) session.persona.prompt = this.persona.prompt;
        if (Array.isArray(this.persona.aliases) && this.persona.aliases.length) {
          session.persona.aliases = this.persona.aliases;
        }
        if (this.instructions) session.instructions = this.instructions;
        this._sendJson({ action: 'session.update', session });
        break;
      }
      case 'session.updated': {
        if (data.error) {
          this._fail('session_config_failed', `会话配置失败：${data.error}`);
          return;
        }
        this._sendJson({ action: 'media.mode', binary_protocol: 'tagged-v1' });
        this._setState(SessionState.READY);
        break;
      }
      case 'input.speech.started':
        this._emit('ui.state', { kind: 'listening' });
        break;
      case 'input.speech.stopped':
        this._lastSpeechEndAt = performance.now();
        this._emit('ui.state', { kind: 'thinking' });
        break;
      case 'response.audio.started': {
        this._setState(SessionState.LIVE);
        if (this._lastSpeechEndAt) {
          this.metrics.lastResponseMs = Math.round(performance.now() - this._lastSpeechEndAt);
          this._lastSpeechEndAt = null;
        }
        this._emit('ui.state', { kind: 'speaking' });
        break;
      }
      case 'response.audio.context':
        this.pendingInfo = {
          chunkId: data.chunk_id,
          runId: data.run_id,
          turnId: data.turn_id,
        };
        break;
      case 'response.audio.stop': {
        const { playedMs, info } = this.player.stop();
        const target = {
          runId: data.run_id || (info && info.runId),
          turnId: data.turn_id || (info && info.turnId),
        };
        this._sendJson({
          action: 'playback.stopped',
          run_id: target.runId,
          turn_id: target.turnId,
          played_audio_ms: Math.max(0, Math.round(playedMs)),
        });
        this._purgeRun(data.run_id);
        this._emit('ui.state', { kind: 'listening' });
        break;
      }
      case 'response.interrupted': {
        this._purgeRun(data.run_id);
        this._emit('ui.interrupted', { text: data.text || '', runId: data.run_id });
        break;
      }
      case 'response.audio.done': {
        const run = this.runs.get(data.run_id);
        if (run) {
          run.done = true;
          if (run.pending.size === 0) {
            this._completeRun(data.run_id, run);
          }
        }
        break;
      }
      case 'response.ended':
        this._emit('ui.turn_end', { status: data.status, runId: data.run_id });
        if (this.state === SessionState.LIVE) {
          this._setState(SessionState.READY);
        }
        break;
      case 'error': {
        const err = data.error || {};
        const local = Boolean(data.local);
        this.onError({
          code: err.code || 'upstream_error',
          message: err.message || '上游返回错误',
          fatal: !local,
        });
        if (!local) this._fail(err.code || 'upstream_error', err.message || '会话已结束');
        break;
      }
      default:
        break;
    }
  }

  _handleBinary(buffer) {
    const info = this.pendingInfo;
    this.pendingInfo = null;
    if (info) {
      let run = this.runs.get(info.runId);
      if (!run) {
        run = { turnId: info.turnId, pending: new Set(), done: false, completed: false };
        this.runs.set(info.runId, run);
      }
      run.pending.add(info.chunkId);
    }
    if (!this.metrics.firstAudioAt) {
      this.metrics.firstAudioAt = performance.now();
    }
    this.player.enqueue(buffer, info);
  }

  _handleChunkDone(item) {
    if (!item.info) return;
    const { chunkId, runId, turnId } = item.info;
    this._sendJson({
      action: 'playback.chunk.completed',
      chunk_id: chunkId,
      run_id: runId,
      turn_id: turnId,
    });
    const run = this.runs.get(runId);
    if (run) {
      run.pending.delete(chunkId);
      if (run.done && run.pending.size === 0) {
        this._completeRun(runId, run);
      }
    }
  }

  _handleDrain() {
    this._emit('ui.playback_idle', {});
  }

  _completeRun(runId, run) {
    if (run.completed) return;
    run.completed = true;
    this._sendJson({ action: 'playback.completed', run_id: runId, turn_id: run.turnId });
    this.runs.delete(runId);
  }

  _purgeRun(runId) {
    if (runId) this.runs.delete(runId);
  }

  // ------------------------------------------------------------ 内部

  _setState(state) {
    if (this.state === state) return;
    this.state = state;
    this.onState(state);
  }

  _emit(action, data) {
    this.onEvent(action, data);
  }

  _fail(code, message) {
    this._setState(SessionState.FAILED);
    this.onError({ code, message, fatal: true });
    try { this.ws && this.ws.close(4000, 'failed'); } catch (_) { /* noop */ }
  }

  get stats() {
    return {
      sentAudioFrames: this.sentAudioFrames,
      sentVideoFrames: this.sentVideoFrames,
      droppedAudioFrames: this.droppedAudioFrames,
      pendingAudioChunks: this.player.pendingChunks,
      playbackUnderruns: this.player.underruns,
      playbackBacklogSeconds: Math.round(this.player.backlogSeconds * 100) / 100,
      playbackBacklogEvents: this.player.backlogEvents,
      playedChunks: this.player.playedChunks,
      lastResponseMs: this.metrics.lastResponseMs,
    };
  }
}
