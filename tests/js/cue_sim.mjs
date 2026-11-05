// 开场问候音频（cue）回归检查（供 pytest 调用，不依赖浏览器）。
//
//   node cue_sim.mjs <player.js 路径>
//
// 要守住的两件事：
//   1. 问候音频走独立通道，不占用下行队列的 nextTime —— 否则用户一开口，
//      回答会被排到问候后面，白白多等两三秒。
//   2. 被打断时能真的停掉：gain 被 ramp 到 0，source 被 stop。
//
// 与 queue_sim.mjs 一样，把 player.js 转成 data: URL 再 import，避免 Node
// 按 CommonJS 解析浏览器模块。

import { readFileSync } from 'node:fs';

const playerPath = process.argv[2];
if (!playerPath) {
  console.error('用法：node cue_sim.mjs <player.js>');
  process.exit(2);
}

const SAMPLE_RATE = 48000;
const CHUNK_SECONDS = 0.0935;
const CHUNKS = 77;
const CUE_SECONDS = 3.0;
const CUE_DELAY = 0.4;

let clock = 0;
let ctxSeq = 0;
const calls = [];
const ramps = [];

function makeContext() {
  const ctxId = ++ctxSeq;
  return {
    get currentTime() { return clock; },
    state: 'running',
    destination: {},
    createGain() {
      return {
        gain: {
          value: 1,
          cancelScheduledValues() {},
          setValueAtTime() {},
          linearRampToValueAtTime(value) { ramps.push({ ctxId, value }); },
        },
        connect() {},
      };
    },
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
      return {
        length,
        duration: length / SAMPLE_RATE,
        getChannelData() { return new Float32Array(length); },
      };
    },
    createBufferSource() {
      const source = {
        buffer: null,
        onended: null,
        connect() {},
        start(at) {
          const dur = source.buffer.duration;
          calls.push({
            ctxId,
            startAt: at,
            endAt: at + dur,
            isCue: Boolean(source.buffer.isCue),
          });
        },
        stop() { source.stoppedByCue = true; },
      };
      return source;
    },
    decodeAudioData() {
      return Promise.resolve({
        isCue: true,
        duration: CUE_SECONDS,
        length: Math.round(CUE_SECONDS * SAMPLE_RATE),
      });
    },
    resume() { return Promise.resolve(); },
    close() { return Promise.resolve(); },
  };
}

globalThis.window = { AudioContext: function FakeAudioContext() { return makeContext(); } };

const source = readFileSync(playerPath, 'utf8');
const moduleUrl = `data:text/javascript,${encodeURIComponent(source)}`;
const { AudioPlayer } = await import(moduleUrl);

const player = new AudioPlayer({});
player.ensureContext();

// 接通瞬间播问候（延迟 0.4s），随后上游按 8x 速度下发回答
await player.playCue(new ArrayBuffer(1024), CUE_DELAY);
const cueCall = calls.find((c) => c.isCue);
const backlogAfterCue = player.backlogSeconds;

const bytes = Math.round(CHUNK_SECONDS * SAMPLE_RATE) * 2;
for (let i = 0; i < CHUNKS; i += 1) {
  clock = (i * CHUNK_SECONDS) / 8;
  player.enqueue(new ArrayBuffer(bytes), { chunkId: `c${i}`, runId: 'run-1', turnId: 'turn-1' });
}

const downstream = calls.filter((c) => !c.isCue).sort((a, b) => a.startAt - b.startAt);
const firstDownstream = downstream[0];
const expectedFirst = 0 + 0.08;

// 用户开口 / 上游开始回答时打断问候
clock = 0.6;
const stopped = player.stopCue(0.12);
const cueActiveAfterStop = player.cueActive;

console.log(JSON.stringify({
  cueScheduledAt: Number((cueCall ? cueCall.startAt : -1).toFixed(3)),
  cueEndsAt: Number((cueCall ? cueCall.endAt : -1).toFixed(3)),
  cueDelayed: cueCall ? Math.abs(cueCall.startAt - CUE_DELAY) < 1e-6 : false,
  cueTookOverQueue: backlogAfterCue > 0.001,
  downstreamCount: downstream.length,
  firstDownstreamStart: Number((firstDownstream ? firstDownstream.startAt : -1).toFixed(3)),
  expectedFirst: Number(expectedFirst.toFixed(3)),
  queueUntouched: firstDownstream ? Math.abs(firstDownstream.startAt - expectedFirst) < 1e-6 : false,
  stopCueReturned: stopped,
  cueRampedToZero: ramps.some((r) => r.value === 0),
  cueActiveAfterStop,
}));
