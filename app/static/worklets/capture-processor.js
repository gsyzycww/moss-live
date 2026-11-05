/**
 * 麦克风采集处理器：把输入的浮点采样实时重采样为 16 kHz 单声道 PCM S16LE。
 * 输出以 20 ms 为一帧，通过 port 传回主线程（ArrayBuffer）。
 */
class MossCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.frameMs = opts.frameMs || 20;
    this.frameSize = Math.max(1, Math.round((this.targetRate * this.frameMs) / 1000));
    this.ratio = sampleRate / this.targetRate;

    this.frame = new Int16Array(this.frameSize);
    this.frameLen = 0;
    this.buffer = new Float32Array(4096);
    this.bufferLen = 0;
    this.pos = 0;
  }

  _push(value) {
    if (this.bufferLen === this.buffer.length) {
      const next = new Float32Array(this.buffer.length * 2);
      next.set(this.buffer);
      this.buffer = next;
    }
    this.buffer[this.bufferLen++] = value;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true;
    }

    const data = input[0];
    for (let i = 0; i < data.length; i += 1) {
      this._push(data[i]);
    }

    let read = this.pos;
    while (Math.floor(read) + 1 < this.bufferLen) {
      const i0 = Math.floor(read);
      const frac = read - i0;
      const sample = this.buffer[i0] * (1 - frac) + this.buffer[i0 + 1] * frac;
      const clamped = sample > 1 ? 1 : sample < -1 ? -1 : sample;
      this.frame[this.frameLen++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;

      if (this.frameLen >= this.frameSize) {
        const out = this.frame.slice();
        this.port.postMessage(out.buffer, [out.buffer]);
        this.frameLen = 0;
      }
      read += this.ratio;
    }

    const consumed = Math.floor(read);
    if (consumed > 0) {
      this.buffer.copyWithin(0, consumed, this.bufferLen);
      this.bufferLen -= consumed;
      this.pos = read - consumed;
    } else {
      this.pos = read;
    }
    return true;
  }
}

registerProcessor('moss-capture', MossCaptureProcessor);
