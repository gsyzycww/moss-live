// 播放队列排布回归检查（供 pytest 调用，不依赖浏览器）。
//
// 用虚拟时钟把 player.js 的下行音频块按指定速度喂进去，检查排布是否始终
// 顺序、有没有「多块同时播放」或「内容被压缩」。输出一行 JSON 供 Python 断言。
//
//   node queue_sim.mjs <player.js 路径> <倍速>
//
// 背景：上游实测以约 8x 实时速度下发音频（7.2s 回答 / 0.89s 送达）。
// 早期实现为了压延迟，会在队列领先超过 1.5s 时把新块回填到当前时刻，
// 结果与已排好的块同时出声，听起来像"两个声音在说话"、回答被截短。
//
// 说明：player.js 是浏览器端 ES 模块，但目录里没有 package.json，Node 会按
// CommonJS 解析 .js。这里把源码转成 data: URL 再 import，免去复制/改名。

import { readFileSync } from 'node:fs';

const playerPath = process.argv[2];
const speed = Number(process.argv[3] || '8');
if (!playerPath) {
  console.error('用法：node queue_sim.mjs <player.js> <倍速>');
  process.exit(2);
}

const SAMPLE_RATE = 48000;
const CHUNK_SECONDS = 0.0935;                 // 实测平均块长（约 90~100ms）
const CHUNKS = 77;                            // 实测一轮回答的块数
const audioSeconds = CHUNK_SECONDS * CHUNKS;
const arrivalGap = (CHUNK_SECONDS / speed) * 1000;

let clock = 0;
const scheduled = [];

function makeContext() {
  return {
    get currentTime() { return clock; },
    state: 'running',
    destination: {},
    createGain() { return { gain: { value: 1 }, connect() {} }; },
    createAnalyser() {
      return {
        fftSize: 512,
        smoothingTimeConstant: 0.7,
        frequencyBinCount: 256,
        connect() {},
        getByteTimeDomainData() {},
      };
    },
    createBuffer(_channels, length) {
      return { length, getChannelData() { return new Float32Array(length); } };
    },
    createBufferSource() {
      const source = {
        buffer: null,
        onended: null,
        connect() {},
        start(at) {
          scheduled.push({ startAt: at, endAt: at + source.buffer.length / SAMPLE_RATE });
        },
        stop() {},
      };
      return source;
    },
    resume() { return Promise.resolve(); },
    close() { return Promise.resolve(); },
  };
}

globalThis.window = { AudioContext: function FakeAudioContext() { return makeContext(); } };

const source = readFileSync(playerPath, 'utf8');
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`;
const { AudioPlayer } = await import(moduleUrl);
const player = new AudioPlayer({});

const bytes = Math.round(CHUNK_SECONDS * SAMPLE_RATE) * 2;
for (let i = 0; i < CHUNKS; i += 1) {
  clock = (i * arrivalGap) / 1000;
  player.enqueue(new ArrayBuffer(bytes), { chunkId: `c${i}`, runId: 'run-1', turnId: 'turn-1' });
}

scheduled.sort((a, b) => a.startAt - b.startAt);

let peakConcurrency = 0;
let overlaps = 0;
for (let i = 0; i < scheduled.length; i += 1) {
  const current = scheduled[i];
  const concurrent = scheduled.filter(
    (other) => other.startAt <= current.startAt && other.endAt > current.startAt + 1e-9,
  ).length;
  peakConcurrency = Math.max(peakConcurrency, concurrent);
  if (i > 0 && current.startAt < scheduled[i - 1].endAt - 1e-6) overlaps += 1;
}

const span = scheduled.length
  ? scheduled[scheduled.length - 1].endAt - scheduled[0].startAt
  : 0;

console.log(JSON.stringify({
  speed,
  scheduled: scheduled.length,
  expected: CHUNKS,
  peakConcurrency,
  overlaps,
  spanSeconds: Number(span.toFixed(3)),
  audioSeconds: Number(audioSeconds.toFixed(3)),
}));
