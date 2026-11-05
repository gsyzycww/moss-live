/**
 * 麦克风采集：AudioWorklet 实时重采样到 16 kHz PCM S16LE，20 ms 一帧。
 */

const TARGET_RATE = 16000;
const GET_USER_MEDIA_TIMEOUT = 10000;
const RESUME_TIMEOUT = 2500;

function withTimeout(promise, ms, onTimeout) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(onTimeout()), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

export class MicError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'MicError';
    this.code = code;
  }
}

export class MicCapture {
  constructor({ onFrame, onLevel } = {}) {
    this.onFrame = onFrame || (() => {});
    this.onLevel = onLevel || (() => {});
    this.ctx = null;
    this.stream = null;
    this.source = null;
    this.node = null;
    this.sink = null;
    this.enabled = true;
    this.started = false;
    this.level = 0;
  }

  get sampleRate() {
    return this.ctx ? this.ctx.sampleRate : 0;
  }

  async start() {
    if (this.started) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new MicError('insecure_context', '当前浏览器不支持麦克风采集，请使用 HTTPS 或 localhost 访问');
    }

    try {
      this.stream = await withTimeout(navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
        video: false,
      }), GET_USER_MEDIA_TIMEOUT, () => new MicError('mic_timeout', '麦克风启动超时：系统没有响应设备请求'));
    } catch (err) {
      const code = err && err.name ? err.name : 'mic_error';
      const message =
        code === 'NotAllowedError'
          ? '麦克风权限被拒绝，请在浏览器地址栏允许麦克风后重试'
          : code === 'NotFoundError'
            ? '没有检测到可用的麦克风'
            : `无法打开麦克风：${code}`;
      throw new MicError(code, message);
    }

    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.ctx = new Ctx({ latencyHint: 'interactive' });
    if (this.ctx.state === 'suspended') {
      // 某些环境（无声卡 / 自动播放策略）resume 会一直 pending，超时后继续即可
      await withTimeout(this.ctx.resume(), RESUME_TIMEOUT, () => new Error('resume_timeout')).catch(() => {});
    }

    await this.ctx.audioWorklet.addModule('/static/worklets/capture-processor.js');

    this.node = new AudioWorkletNode(this.ctx, 'moss-capture', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: { targetRate: TARGET_RATE, frameMs: 20 },
    });

    this.node.port.onmessage = (event) => {
      if (!this.enabled) return;
      const pcm = new Int16Array(event.data);
      this.level = computeLevel(pcm);
      this.onLevel(this.level);
      this.onFrame(pcm);
    };

    this.source = this.ctx.createMediaStreamSource(this.stream);
    // 静音输出到 destination，保证音频图持续运行（不出声）。
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;
    this.source.connect(this.node);
    this.node.connect(this.sink);
    this.sink.connect(this.ctx.destination);

    this.started = true;
    this.enabled = true;
  }

  setEnabled(enabled) {
    this.enabled = Boolean(enabled);
    if (this.stream) {
      this.stream.getAudioTracks().forEach((track) => {
        track.enabled = Boolean(enabled);
      });
    }
    if (!enabled) {
      this.level = 0;
      this.onLevel(0);
    }
  }

  async close() {
    this.enabled = false;
    this.started = false;
    if (this.node) {
      try { this.node.port.onmessage = null; this.node.disconnect(); } catch (_) { /* noop */ }
      this.node = null;
    }
    if (this.source) {
      try { this.source.disconnect(); } catch (_) { /* noop */ }
      this.source = null;
    }
    if (this.sink) {
      try { this.sink.disconnect(); } catch (_) { /* noop */ }
      this.sink = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach((track) => track.stop());
      this.stream = null;
    }
    if (this.ctx) {
      try { await this.ctx.close(); } catch (_) { /* noop */ }
      this.ctx = null;
    }
  }
}

function computeLevel(int16) {
  if (!int16.length) return 0;
  let sum = 0;
  for (let i = 0; i < int16.length; i += 1) {
    const v = int16[i] / 32768;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / int16.length);
  // 人声 RMS 大约在 0.01 ~ 0.25 之间，映射到 0..1
  return Math.min(1, Math.pow(rms / 0.18, 0.7));
}
