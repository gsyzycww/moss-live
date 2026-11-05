/**
 * 摄像头 / 屏幕共享：按固定帧率把画面缩放、编码成 JPEG 后交给上层发送。
 * 摄像头长边 640 px、屏幕 1024 px、JPEG quality 0.8、1 FPS（与官方协议默认值一致）。
 */

const PRESET = {
  camera: { longEdge: 640, fps: 1, quality: 0.8 },
  screen: { longEdge: 1024, fps: 1, quality: 0.8 },
};

export class VideoError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'VideoError';
    this.code = code;
  }
}

export class VideoSender {
  constructor({ onFrame, onEnded, onError } = {}) {
    this.onFrame = onFrame || (() => {});
    this.onEnded = onEnded || (() => {});
    this.onError = onError || (() => {});
    this.sources = new Map(); // source -> { stream, video, timer, canvas, ctx, busy }
  }

  isActive(source) {
    return this.sources.has(source);
  }

  async start(source, { deviceId } = {}) {
    if (this.sources.has(source)) return;
    if (!navigator.mediaDevices) {
      throw new VideoError('unsupported', '当前浏览器不支持媒体采集，请使用 HTTPS 或 localhost 访问');
    }

    let stream;
    try {
      if (source === 'screen') {
        if (!navigator.mediaDevices.getDisplayMedia) {
          throw new VideoError('screen_unsupported', '当前浏览器不支持屏幕共享');
        }
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: { frameRate: 10 },
          audio: false,
        });
      } else {
        const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
        if (deviceId) video.deviceId = { exact: deviceId };
        else video.facingMode = 'user';
        stream = await navigator.mediaDevices.getUserMedia({ video, audio: false });
      }
    } catch (err) {
      if (err instanceof VideoError) throw err;
      const code = err && err.name ? err.name : 'media_error';
      const message =
        code === 'NotAllowedError'
          ? source === 'screen'
            ? '屏幕共享被取消或未授权'
            : '摄像头权限被拒绝，请在浏览器地址栏允许摄像头'
          : code === 'NotFoundError'
            ? '没有检测到可用的摄像头'
            : `无法打开${source === 'screen' ? '屏幕共享' : '摄像头'}：${code}`;
      throw new VideoError(code, message);
    }

    const videoEl = document.createElement('video');
    videoEl.autoplay = true;
    videoEl.muted = true;
    videoEl.playsInline = true;
    videoEl.srcObject = stream;
    videoEl.style.position = 'fixed';
    videoEl.style.opacity = '0';
    videoEl.style.pointerEvents = 'none';
    videoEl.style.width = '1px';
    videoEl.style.height = '1px';
    document.body.appendChild(videoEl);

    try {
      await videoEl.play();
    } catch (_) {
      /* 某些浏览器需要用户手势，静默失败即可 */
    }

    const preset = PRESET[source] || PRESET.camera;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d', { alpha: false });
    const entry = { stream, video: videoEl, canvas, ctx, timer: null, busy: false, preset, size: null };
    this.sources.set(source, entry);

    stream.getVideoTracks().forEach((track) => {
      track.addEventListener('ended', () => {
        if (!this.sources.has(source)) return;
        this.stop(source);
        this.onEnded(source);
      });
    });

    const tick = () => {
      const current = this.sources.get(source);
      if (!current || current.busy) return;
      const { videoWidth, videoHeight } = videoEl;
      if (!videoWidth || !videoHeight) return;

      const target = current.preset.longEdge;
      const scale = Math.min(1, target / Math.max(videoWidth, videoHeight));
      const w = Math.max(2, Math.round(videoWidth * scale));
      const h = Math.max(2, Math.round(videoHeight * scale));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      current.size = { width: w, height: h };

      try {
        ctx.drawImage(videoEl, 0, 0, w, h);
      } catch (_) {
        return;
      }

      current.busy = true;
      canvas.toBlob(
        (blob) => {
          current.busy = false;
          if (!blob || !this.sources.has(source)) return;
          blob
            .arrayBuffer()
            .then((buf) => {
              if (!this.sources.has(source)) return;
              this.onFrame(source, new Uint8Array(buf));
            })
            .catch((err) => this.onError(err));
        },
        'image/jpeg',
        current.preset.quality,
      );
    };

    entry.timer = setInterval(tick, Math.round(1000 / preset.fps));
    tick();
  }

  stop(source) {
    const entry = this.sources.get(source);
    if (!entry) return;
    this.sources.delete(source);
    if (entry.timer) clearInterval(entry.timer);
    try { entry.stream.getTracks().forEach((t) => t.stop()); } catch (_) { /* noop */ }
    try { entry.video.srcObject = null; } catch (_) { /* noop */ }
    try { entry.video.remove(); } catch (_) { /* noop */ }
  }

  getStream(source) {
    const entry = this.sources.get(source);
    return entry ? entry.stream : null;
  }

  stopAll() {
    Array.from(this.sources.keys()).forEach((source) => this.stop(source));
  }

  get size() {
    const out = {};
    this.sources.forEach((entry, source) => {
      out[source] = entry.size;
    });
    return out;
  }
}
