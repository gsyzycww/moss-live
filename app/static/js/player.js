/**
 * 下行音频播放器：48 kHz 单声道 PCM S16LE 分块入队，
 * 按“实际播放完成”回执，并支持被服务端打断时立即停止。
 */

const SAMPLE_RATE = 48000;
const START_DELAY = 0.08;  // 80ms 起步缓冲，避免网络抖动造成爆音
const BACKLOG_WARN = 1.5;  // 队列领先超过该值只做计数，绝不重排（见 enqueue 注释）

export class AudioPlayer {
  constructor({ onChunkDone, onDrain, onLevel, onCueDone } = {}) {
    this.onChunkDone = onChunkDone || (() => {});
    this.onDrain = onDrain || (() => {});
    this.onLevel = onLevel || (() => {});
    this.onCueDone = onCueDone || (() => {});
    this.ctx = null;
    this.gain = null;
    this.analyser = null;
    this.levelData = null;
    this.items = new Set();
    this.nextTime = 0;
    this.muted = false;
    this.underruns = 0;
    this.backlogEvents = 0;
    this.playedChunks = 0;
    this.cue = null;
    this._cueToken = 0;
  }

  ensureContext() {
    if (this.ctx) return this.ctx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.ctx = new Ctx({ latencyHint: 'interactive' });
    this.gain = this.ctx.createGain();
    this.gain.gain.value = this.muted ? 0 : 1;
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.analyser.smoothingTimeConstant = 0.7;
    this.levelData = new Uint8Array(this.analyser.frequencyBinCount);
    this.gain.connect(this.analyser);
    this.analyser.connect(this.ctx.destination);
    this.nextTime = this.ctx.currentTime;
    return this.ctx;
  }

  async resume() {
    const ctx = this.ensureContext();
    if (ctx.state === 'suspended') {
      try { await ctx.resume(); } catch (_) { /* noop */ }
    }
  }

  get context() {
    return this.ctx;
  }

  /**
   * @param {ArrayBuffer} buffer 48 kHz PCM S16LE（不含任何前缀字节）
   * @param {{chunkId:string, runId:string, turnId:string}|null} info 音频块上下文
   */
  enqueue(buffer, info) {
    const ctx = this.ensureContext();
    const totalSamples = Math.floor(buffer.byteLength / 2);
    if (totalSamples <= 0) return;

    const pcm = new Int16Array(buffer, 0, totalSamples);
    const audioBuffer = ctx.createBuffer(1, totalSamples, SAMPLE_RATE);
    const channel = audioBuffer.getChannelData(0);
    for (let i = 0; i < totalSamples; i += 1) {
      channel[i] = pcm[i] / 32768;
    }

    const source = ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.gain);

    const now = ctx.currentTime;

    // 上一块已经播完、队列是空的：说明中间出现了播放空洞（真实欠载）。
    if (this.nextTime > 0 && this.nextTime < now - 0.05) {
      this.underruns += 1;
    }

    // 严格单调：新块一律排在已排队音频之后，永不回填到当前时刻。
    //
    // 上游实测以约 8x 实时速度下发（7.2s 回答音频 0.89s 就送完 77 个块），
    // 队列必然大幅领先实时，这是正常现象：AudioBufferSource 按 AudioContext
    // 时钟精确排布，领先多少都不会漂移。一旦为了"压缩延迟"把新块回填到
    // now + START_DELAY，它就会和已经排好的块同时出声——表现为两三个声音
    // 叠在一起，且整段音频被压短、丢掉后半段内容。
    const startAt = Math.max(now + START_DELAY, this.nextTime);
    if (startAt - now > BACKLOG_WARN) {
      this.backlogEvents += 1;
    }

    const duration = totalSamples / SAMPLE_RATE;
    const item = {
      source,
      info,
      startAt,
      endAt: startAt + duration,
      durationMs: duration * 1000,
      stopped: false,
      done: false,
    };
    this.items.add(item);
    this.nextTime = startAt + duration;

    source.onended = () => {
      if (item.stopped || item.done) return;
      item.done = true;
      this.items.delete(item);
      this.playedChunks += 1;
      this.onChunkDone(item);
      if (this.items.size === 0) {
        this.onDrain();
      }
    };

    try {
      source.start(startAt);
    } catch (_) {
      this.items.delete(item);
    }
  }

  /**
   * 立即停止播放，返回当前块已播放的毫秒数。
   */
  stop() {
    const ctx = this.ctx;
    let playedMs = 0;
    let lastInfo = null;
    if (ctx) {
      const now = ctx.currentTime;
      for (const item of this.items) {
        item.stopped = true;
        if (!lastInfo && item.info) lastInfo = item.info;
        if (now >= item.startAt && now <= item.endAt) {
          playedMs = Math.max(0, Math.round((now - item.startAt) * 1000));
          lastInfo = item.info || lastInfo;
        }
        try { item.source.stop(); } catch (_) { /* noop */ }
      }
      this.items.clear();
      this.nextTime = now;
    }
    return { playedMs, info: lastInfo };
  }

  /**
   * 播放一段本地音频（开场问候），与下行队列完全独立：不占用 nextTime、
   * 不参与回执，所以上游音频随时可以照常排期，不会互相排队。
   *
   * @param {ArrayBuffer} buffer WAV/MP3 等浏览器可解码的容器
   * @param {number} delaySeconds 相对当前时刻的延迟
   * @returns {Promise<number>} 音频时长（秒）；被取消或解码失败返回 0
   */
  async playCue(buffer, delaySeconds = 0.4) {
    this.stopCue(0);
    // 上下文已被 close()（挂断）就不再新建，避免在通话结束后留下一个空 AudioContext
    if (!this.ctx) return 0;
    const ctx = this.ctx;
    const token = (this._cueToken += 1);

    let decoded;
    try {
      decoded = await ctx.decodeAudioData(buffer.slice(0));
    } catch (_) {
      return 0;
    }
    // 解码期间可能已被打断、或整条通话已关闭
    if (token !== this._cueToken || this.ctx !== ctx || !this.gain) return 0;

    const gain = ctx.createGain();
    gain.gain.value = 1;
    gain.connect(this.gain);

    const source = ctx.createBufferSource();
    source.buffer = decoded;
    source.connect(gain);

    const startAt = ctx.currentTime + Math.max(0, delaySeconds);
    const cue = { source, gain, startAt, endAt: startAt + decoded.duration, stopped: false };
    this.cue = cue;

    source.onended = () => {
      if (cue.stopped) return;
      cue.stopped = true;
      if (this.cue === cue) this.cue = null;
      this.onCueDone({ durationMs: Math.round(decoded.duration * 1000) });
    };

    try {
      source.start(startAt);
    } catch (_) {
      this.cue = null;
      return 0;
    }
    return decoded.duration;
  }

  /** 淡出并停止本地音频（用户开口或上游开始回答时调用）。 */
  stopCue(fadeSeconds = 0.12) {
    this._cueToken += 1;
    const cue = this.cue;
    if (!cue) return false;
    this.cue = null;
    cue.stopped = true;
    const ctx = this.ctx;
    if (!ctx) return true;
    const now = ctx.currentTime;
    try {
      cue.gain.gain.cancelScheduledValues(now);
      cue.gain.gain.setValueAtTime(cue.gain.gain.value, now);
      cue.gain.gain.linearRampToValueAtTime(0, now + fadeSeconds);
      cue.source.stop(now + fadeSeconds + 0.02);
    } catch (_) { /* noop */ }
    return true;
  }

  /** 本地音频是否正在播放（含尚未到起播时刻的排期）。 */
  get cueActive() {
    return Boolean(this.cue);
  }

  clear() {
    this.stop();
    this.playedChunks = 0;
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    if (this.gain) {
      this.gain.gain.value = this.muted ? 0 : 1;
    }
  }

  getLevel() {
    if (!this.analyser) return 0;
    this.analyser.getByteTimeDomainData(this.levelData);
    let sum = 0;
    for (let i = 0; i < this.levelData.length; i += 1) {
      const v = (this.levelData[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / this.levelData.length);
    return Math.min(1, Math.pow(rms / 0.2, 0.7));
  }

  get pendingChunks() {
    return this.items.size;
  }

  /** 队列领先实时播放的秒数（上游突发下发时会有几秒，属正常）。 */
  get backlogSeconds() {
    if (!this.ctx) return 0;
    return Math.max(0, this.nextTime - this.ctx.currentTime);
  }

  async close() {
    this.stopCue(0);
    this.clear();
    if (this.ctx) {
      try { await this.ctx.close(); } catch (_) { /* noop */ }
      this.ctx = null;
      this.gain = null;
      this.analyser = null;
    }
  }
}
