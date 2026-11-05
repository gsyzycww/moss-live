/**
 * 应用主控制器：落地页交互 + 通话状态机 + 媒体编排 + 字幕/记录/调试面板。
 */

import { MicCapture } from './audio-capture.js';
import { VideoSender } from './media.js';
import { RealtimeSession, SessionState } from './session.js';
import { applyStaticI18n, getLang, langButtonLabel, setLang, t } from './i18n.js';

const LS = {
  lang: 'moss.lang',
  overrides: 'moss.persona.overrides',
  persona: 'moss.persona.active',
  scenario: 'moss.scenario',
  user: 'moss.user',
};

const $ = (id) => document.getElementById(id);

const el = {
  statusPill: $('statusPill'),
  langBtn: $('langBtn'),
  stage: $('characterStage'),
  scenarioRow: $('scenarioRow'),
  version: $('versionText'),
  call: $('call'),
  callStatus: $('callStatus'),
  callTimer: $('callTimer'),
  callOrb: $('callOrb'),
  callName: $('callName'),
  callState: $('callState'),
  levelFill: $('levelFill'),
  videoDock: $('videoDock'),
  cameraTile: $('cameraTile'),
  cameraVideo: $('cameraVideo'),
  screenTile: $('screenTile'),
  screenVideo: $('screenVideo'),
  capUser: $('capUser'),
  capAi: $('capAi'),
  capAiTag: $('capAiTag'),
  micBtn: $('micBtn'),
  camBtn: $('camBtn'),
  screenBtn: $('screenBtn'),
  focusBtn: $('focusBtn'),
  focusLabel: $('focusLabel'),
  endBtn: $('endBtn'),
  endBtnTop: $('endBtnTop'),
  transcriptBtn: $('transcriptBtn'),
  settingsBtn: $('settingsBtn'),
  transcriptPanel: $('transcriptPanel'),
  transcriptBody: $('transcriptBody'),
  downloadBtn: $('downloadBtn'),
  closeTranscript: $('closeTranscript'),
  tpStats: $('tpStats'),
  debugPanel: $('debugPanel'),
  debugBody: $('debugBody'),
  clearDebug: $('clearDebug'),
  closeDebug: $('closeDebug'),
  debugBtn: $('debugBtn'),
  toastWrap: $('toastWrap'),
  settingsModal: $('settingsModal'),
  setName: $('setName'),
  setPrompt: $('setPrompt'),
  setInstructions: $('setInstructions'),
  setScenario: $('setScenario'),
  resetPersona: $('resetPersona'),
  savePersona: $('savePersona'),
  closeSettings: $('closeSettings'),
};

const state = {
  lang: 'zh',
  config: null,
  personas: [],
  scenarios: [],
  activePersonaId: localStorage.getItem(LS.persona) || 'luna',
  scenarioId: localStorage.getItem(LS.scenario) || 'chat',
  overrides: loadJSON(LS.overrides, {}),
  accessCode: new URLSearchParams(location.search).get('code') || '',
  call: null,
  debug: { rows: [], open: false },
  escArmed: 0,
};

// ---------------------------------------------------------------- 工具

function loadJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (_) {
    return fallback;
  }
}

function saveJSON(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* noop */ }
}

function userId() {
  let id = localStorage.getItem(LS.user);
  if (!id || !/^[A-Za-z0-9_.:-]{1,64}$/.test(id)) {
    const rand = Math.random().toString(16).slice(2, 10);
    id = `web-${rand}`;
    localStorage.setItem(LS.user, id);
  }
  return id;
}

function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = String(Math.floor(total / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return `${m}:${s}`;
}

function toast(message, kind = 'info', timeout = 2800) {
  const node = document.createElement('div');
  node.className = 'toast';
  node.dataset.kind = kind;
  node.textContent = message;
  el.toastWrap.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .25s ease';
    setTimeout(() => node.remove(), 260);
  }, timeout);
}

// ---------------------------------------------------------------- 启动

async function boot() {
  const saved = localStorage.getItem(LS.lang);
  state.lang = saved || ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en');
  setLang(state.lang);
  applyStaticI18n();
  el.langBtn.textContent = langButtonLabel();

  bindGlobalEvents();
  await loadConfig();
  checkHealth();
  setInterval(checkHealth, 30000);
}

async function loadConfig() {
  try {
    const res = await fetch(`/api/config?lang=${state.lang}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cfg = await res.json();
    state.config = cfg;
    state.personas = cfg.personas || [];
    state.scenarios = cfg.scenarios || [];
    if (!state.personas.some((p) => p.id === state.activePersonaId)) {
      state.activePersonaId = state.personas[0] ? state.personas[0].id : 'luna';
    }
    if (!state.scenarios.some((s) => s.id === state.scenarioId)) {
      state.scenarioId = state.scenarios[0] ? state.scenarios[0].id : 'chat';
    }
    renderStage();
    renderScenarioChips();
    if (el.version) el.version.textContent = `v${cfg.version || '1.0.0'}`;
  } catch (err) {
    toast(`配置加载失败：${err.message}`, 'error');
  }
}

async function checkHealth() {
  try {
    const res = await fetch('/api/health', { cache: 'no-store' });
    const data = await res.json();
    if (!data.api_key_configured) {
      el.statusPill.dataset.state = 'warn';
      el.statusPill.querySelector('.status-text').textContent = t('status.warn');
    } else if (data.status === 'ok') {
      el.statusPill.dataset.state = 'ok';
      el.statusPill.querySelector('.status-text').textContent = t('status.ok');
    } else {
      el.statusPill.dataset.state = 'bad';
      el.statusPill.querySelector('.status-text').textContent = t('status.bad');
    }
  } catch (_) {
    el.statusPill.dataset.state = 'bad';
    el.statusPill.querySelector('.status-text').textContent = t('status.bad');
  }
}

// ---------------------------------------------------------------- 落地页

function renderStage() {
  el.stage.innerHTML = '';
  state.personas.forEach((persona) => {
    const card = document.createElement('div');
    card.className = 'character';
    card.dataset.persona = persona.id;
    card.dataset.tint = persona.tint;
    card.dataset.focused = persona.id === state.activePersonaId ? 'true' : 'false';

    const orb = document.createElement('div');
    orb.className = 'orb';
    orb.dataset.tint = persona.tint;
    orb.innerHTML = '<span class="orb-halo"></span><span class="orb-core"></span><span class="orb-ring"></span>';
    card.appendChild(orb);

    const body = document.createElement('div');
    body.className = 'character-body';
    const override = state.overrides[persona.id] || {};
    const name = document.createElement('p');
    name.className = 'character-name';
    name.textContent = override.name || persona.name;
    const tagline = document.createElement('p');
    tagline.className = 'character-tagline';
    tagline.textContent = persona.tagline;
    body.appendChild(name);
    body.appendChild(tagline);
    card.appendChild(body);

    const bubble = document.createElement('button');
    bubble.type = 'button';
    bubble.className = 'call-bubble';
    bubble.setAttribute('aria-label', `${t('hero.cta')} · ${persona.name}`);
    bubble.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-phone"/></svg>';
    bubble.addEventListener('click', (event) => {
      event.stopPropagation();
      startCall(persona.id);
    });
    card.appendChild(bubble);

    card.addEventListener('click', () => {
      state.activePersonaId = persona.id;
      localStorage.setItem(LS.persona, persona.id);
      document.querySelectorAll('.character').forEach((node) => {
        node.dataset.focused = node.dataset.persona === persona.id ? 'true' : 'false';
      });
    });
    card.addEventListener('dblclick', () => startCall(persona.id));

    el.stage.appendChild(card);
  });
}

function renderScenarioChips() {
  el.scenarioRow.innerHTML = '';
  state.scenarios.forEach((scenario) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.dataset.scenario = scenario.id;
    chip.setAttribute('aria-pressed', scenario.id === state.scenarioId ? 'true' : 'false');
    chip.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-${iconFor(scenario.icon)}"/></svg><span></span>`;
    chip.querySelector('span').textContent = scenario.name;
    chip.addEventListener('click', () => {
      state.scenarioId = scenario.id;
      localStorage.setItem(LS.scenario, scenario.id);
      renderScenarioChips();
      if (state.call) toast(t('set.note'), 'info', 3200);
    });
    el.scenarioRow.appendChild(chip);
  });
}

function iconFor(name) {
  switch (name) {
    case 'translate': return 'translate';
    case 'eye': return 'eye';
    case 'spark': return 'spark';
    default: return 'caption';
  }
}

function effectivePersona() {
  const base = state.personas.find((p) => p.id === state.activePersonaId) || state.personas[0] || {
    id: 'luna', name: 'Luna', prompt: '', instructions: '', tint: 'warm',
  };
  const override = state.overrides[base.id] || {};
  const scenario = state.scenarios.find((s) => s.id === state.scenarioId);
  const instructions = [scenario && scenario.instructions, override.instructions ?? base.instructions]
    .filter((text) => text && String(text).trim())
    .join('\n');
  return {
    id: base.id,
    tint: base.tint || 'warm',
    name: (override.name || base.name || 'Luna').slice(0, 64),
    prompt: (override.prompt ?? base.prompt ?? '').slice(0, 8000),
    instructions: instructions.slice(0, 8000),
    base,
  };
}

// ---------------------------------------------------------------- 通话

// 开场问候音频由 scripts/gen_greeting.py 用模型自己的声音预生成，
// 存放在 app/static/audio/。接通后延迟播放，用户一开口立刻淡出。
const greetingCache = new Map();

function prepareGreeting(personaId, lang) {
  const key = `${personaId}.${lang}`;
  if (!greetingCache.has(key)) {
    const url = `/static/audio/greeting-${personaId}-${lang}.wav`;
    const task = fetch(url, { cache: 'force-cache' })
      .then((res) => (res.ok ? res.arrayBuffer() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .catch(() => null); // 没有预生成文件就静默降级，不影响通话
    greetingCache.set(key, task);
  }
  return greetingCache.get(key);
}

async function playGreeting() {
  const call = state.call;
  if (!call || call.greetingDone || !call.session) return;
  const cfg = state.config && state.config.greeting;
  if (!cfg || !cfg.enabled) return;
  // 预录音频里念的是默认名字；用户改过名字时播出来会叫错人，直接跳过
  if (call.persona.base && call.persona.name !== call.persona.base.name) return;

  call.greetingDone = true;
  const buffer = await prepareGreeting(call.persona.id, state.lang);
  // 等音频期间用户可能已经开口，这时不该再抢话
  if (!buffer || state.call !== call || call.greetingCancelled) return;

  const duration = await call.session.playCue(buffer, (cfg.delay_ms || 400) / 1000);
  if (call.greetingCancelled) {
    call.session.stopCue(0);
    return;
  }
  if (duration > 0) {
    el.callOrb.dataset.speaking = 'true';
    setCallStatus('status.speaking');
  }
}

function stopGreeting(fade = 0.12) {
  const call = state.call;
  if (!call) return;
  call.greetingCancelled = true;
  if (call.session) call.session.stopCue(fade);
}

async function startCall(personaId) {
  if (state.call) return;
  if (personaId) {
    state.activePersonaId = personaId;
    localStorage.setItem(LS.persona, personaId);
    renderStage();
  }
  const persona = effectivePersona();

  const call = {
    persona,
    session: null,
    mic: null,
    video: new VideoSender({
      onFrame: (source, bytes) => state.call && state.call.session && state.call.session.sendVideo(source, bytes),
      onEnded: (source) => handleVideoEnded(source),
      onError: (err) => toast(`画面发送失败：${err.message}`, 'error'),
    }),
    micActive: false,
    muted: false,
    camOn: false,
    screenOn: false,
    focus: 'auto',
    startedAt: Date.now(),
    timerId: null,
    raf: 0,
    smoothLevel: 0,
    transcript: [],
    live: { user: null, ai: null },
    turns: 0,
    statusKey: 'call.connecting',
    ended: false,
    greetingDone: false,
    greetingCancelled: false,
  };
  state.call = call;

  // 提前把问候音频取回来，等会话就绪时直接用，省掉打开连接后的等待
  prepareGreeting(persona.id, state.lang);

  document.body.style.overflow = 'hidden';
  el.call.hidden = false;
  el.call.dataset.tint = persona.tint;
  el.callOrb.dataset.tint = persona.tint;
  el.callOrb.dataset.speaking = 'false';
  el.callName.textContent = persona.name;
  el.capAiTag.textContent = persona.name;
  el.capUser.hidden = true;
  el.capUser.querySelector('.cap-text').textContent = '';
  el.capAi.querySelector('.cap-text').textContent = '';
  el.callTimer.textContent = '00:00';
  el.transcriptBody.innerHTML = `<p class="tp-empty">${t('tp.empty')}</p>`;
  el.tpStats.textContent = '0';
  resetVideoUI();
  setCallStatus('call.connecting');
  el.micBtn.dataset.on = 'true';
  el.micBtn.setAttribute('aria-pressed', 'true');
  el.camBtn.dataset.on = 'false';
  el.screenBtn.dataset.on = 'false';
  el.focusBtn.hidden = true;

  call.timerId = setInterval(() => {
    if (!state.call) return;
    el.callTimer.textContent = formatDuration(Date.now() - state.call.startedAt);
  }, 500);
  loopLevels();

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL(`${proto}//${location.host}/ws/realtime`);
  url.searchParams.set('user', userId());
  if (state.accessCode) url.searchParams.set('code', state.accessCode);

  const session = new RealtimeSession({
    url: url.toString(),
    persona: { name: persona.name, prompt: persona.prompt },
    instructions: persona.instructions,
    onEvent: (action, data) => handleSessionEvent(action, data),
    onState: (status) => handleSessionState(status),
    onError: (err) => handleSessionError(err),
  });
  call.session = session;

  // 仍然在点击手势内：先创建并 resume 播放上下文，避免自动播放策略静音
  try { await session.player.resume(); } catch (_) { /* noop */ }

  // 再拿麦克风（需要用户手势），失败则降级为只听模式
  try {
    call.mic = new MicCapture({
      onFrame: (pcm) => {
        if (!state.call || !state.call.session) return;
        state.call.session.sendAudio(pcm);
      },
    });
    await call.mic.start();
    call.micActive = true;
  } catch (err) {
    call.micActive = false;
    el.micBtn.dataset.on = 'false';
    el.micBtn.setAttribute('aria-pressed', 'false');
    el.micBtn.disabled = true;
    toast(err.message || t('toast.noMic'), 'error', 5200);
  }

  if (!state.call) {
    // 用户在权限弹窗期间结束了通话，释放已申请的麦克风
    if (call.mic) await call.mic.close();
    return;
  }

  session.connect();
}

function handleSessionState(status) {
  const call = state.call;
  if (!call) return;
  if (status === SessionState.READY) {
    setCallStatus('status.listening');
    call.session.setMicEnabled(!call.muted);
    if (call.micActive) call.mic.setEnabled(!call.muted);
    playGreeting();
  } else if (status === SessionState.CONNECTING) {
    setCallStatus('call.connecting');
  }
}

function handleSessionError(err) {
  const call = state.call;
  if (!call) return;
  toast(`${t('err.call')}：${err.message}`, 'error', 5200);
  if (err.fatal) {
    endCall('error');
  }
}

function handleSessionEvent(action, data) {
  const call = state.call;
  if (!call) return;
  logDebug(action.startsWith('client.') || action.startsWith('ui.') ? (action.startsWith('client.') ? 'up' : 'sys') : 'down', action, data);

  switch (action) {
    case 'input.speech.started': {
      stopGreeting();
      el.callOrb.dataset.speaking = 'false';
      setCallStatus('status.listening');
      break;
    }
    case 'input.speech.stopped': {
      setCallStatus('status.thinking');
      break;
    }
    case 'input.transcript.updated': {
      setCaption('user', data.text || '');
      updateLiveItem('user', data.text || '');
      break;
    }
    case 'input.transcript.done': {
      setCaption('user', data.text || '');
      finalizeItem('user', data.text || '');
      break;
    }
    case 'output.transcript.updated': {
      setCaption('ai', data.text || '');
      updateLiveItem('ai', data.text || '');
      break;
    }
    case 'output.transcript.done': {
      setCaption('ai', data.text || '');
      finalizeItem('ai', data.text || '');
      break;
    }
    case 'response.audio.started': {
      stopGreeting();
      el.callOrb.dataset.speaking = 'true';
      setCallStatus('status.speaking');
      break;
    }
    case 'ui.cue_done': {
      // 问候播完，回到等待用户开口的状态
      el.callOrb.dataset.speaking = 'false';
      setCallStatus('status.listening');
      break;
    }
    case 'response.audio.done': {
      break;
    }
    case 'response.interrupted': {
      el.callOrb.dataset.speaking = 'false';
      markInterrupted();
      toast(t('toast.interrupted'), 'info', 1800);
      break;
    }
    case 'response.ended': {
      el.callOrb.dataset.speaking = 'false';
      if (!data || data.status === 'completed') {
        setCallStatus('status.listening');
      }
      break;
    }
    case 'ui.state': {
      const kind = data && data.kind;
      if (kind === 'listening') setCallStatus('status.listening');
      else if (kind === 'thinking') setCallStatus('status.thinking');
      else if (kind === 'speaking') setCallStatus('status.speaking');
      break;
    }
    case 'ui.playback_idle': {
      el.callOrb.dataset.speaking = 'false';
      break;
    }
    default:
      break;
  }
}

function setCallStatus(key) {
  const call = state.call;
  if (!call) return;
  if (call.statusKey === key && el.callStatus.textContent === t(key)) return;
  call.statusKey = key;
  const text = t(key);
  el.callStatus.textContent = text;
  if (el.callState) el.callState.textContent = text;
}

function setCaption(role, text) {
  const node = role === 'user' ? el.capUser : el.capAi;
  const textNode = node.querySelector('.cap-text');
  textNode.textContent = text;
  node.dataset.empty = text ? 'false' : 'true';
  if (role === 'user') node.hidden = !text;
}

// ------------------------------------------------------------ 记录

function resetLive() {
  const call = state.call;
  if (!call) return;
  call.live = { user: null, ai: null };
}

function updateLiveItem(role, text) {
  const call = state.call;
  if (!call || !text) return;
  if (!call.live[role]) {
    call.live[role] = createTranscriptItem(role, text);
    call.live[role].classList.add('is-live');
  } else {
    call.live[role].querySelector('p').textContent = text;
  }
  scrollTranscript();
}

function finalizeItem(role, text) {
  const call = state.call;
  if (!call) return;
  if (!text) {
    if (call.live[role]) {
      call.live[role].remove();
      call.live[role] = null;
    }
    return;
  }
  let node = call.live[role];
  if (!node) node = createTranscriptItem(role, text);
  node.classList.remove('is-live');
  node.querySelector('p').textContent = text;
  call.live[role] = null;
  const record = { role, text, at: Date.now(), interrupted: false };
  call.transcript.push(record);
  node.dataset.record = String(call.transcript.length - 1);
  if (role === 'ai') {
    call.turns += 1;
    el.tpStats.textContent = String(call.turns);
  }
  scrollTranscript();
}

function createTranscriptItem(role, text) {
  const wrap = document.createElement('div');
  wrap.className = `tp-item is-${role === 'user' ? 'user' : 'ai'}`;
  const head = document.createElement('div');
  head.className = 'tp-item-head';
  const who = document.createElement('span');
  who.className = role === 'user' ? 'who-user' : 'who-ai';
  who.textContent = role === 'user' ? t('tp.you') : (state.call ? state.call.persona.name : t('tp.assistant'));
  const time = document.createElement('span');
  time.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  head.appendChild(who);
  head.appendChild(time);
  const p = document.createElement('p');
  p.textContent = text;
  wrap.appendChild(head);
  wrap.appendChild(p);
  const empty = el.transcriptBody.querySelector('.tp-empty');
  if (empty) empty.remove();
  el.transcriptBody.appendChild(wrap);
  return wrap;
}

function scrollTranscript() {
  if (!el.transcriptPanel.hidden) {
    el.transcriptBody.scrollTop = el.transcriptBody.scrollHeight;
  }
}

function markInterrupted() {
  const call = state.call;
  if (!call) return;
  const last = [...call.transcript].reverse().find((item) => item.role === 'ai');
  if (last) last.interrupted = true;
  const nodes = el.transcriptBody.querySelectorAll('.tp-item.is-ai');
  const node = nodes[nodes.length - 1];
  if (node) node.classList.add('is-interrupted');
  if (call.live.ai) {
    call.live.ai.classList.add('is-interrupted');
  }
}

function exportTranscript() {
  const call = state.call;
  if (!call || call.transcript.length === 0) {
    toast(t('toast.nothing'), 'error');
    return;
  }
  const lines = [
    `# ${call.persona.name} · MOSS-Live`,
    `开始时间：${new Date(call.startedAt).toLocaleString()}`,
    '',
  ];
  call.transcript.forEach((item) => {
    const who = item.role === 'user' ? '你' : call.persona.name;
    const mark = item.interrupted ? '（被打断）' : '';
    lines.push(`[${new Date(item.at).toLocaleTimeString()}] ${who}${mark}：${item.text}`);
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `moss-live-${new Date(call.startedAt).toISOString().slice(0, 19).replace(/[:T]/g, '-')}.md`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast(t('toast.downloaded'), 'ok');
}

// ------------------------------------------------------------ 音量与光球

function loopLevels() {
  const call = state.call;
  if (!call) return;
  const micLevel = call.mic && call.micActive && !call.muted ? call.mic.level : 0;
  const outLevel = call.session ? call.session.player.getLevel() : 0;
  const level = Math.max(micLevel * 0.9, outLevel);
  call.smoothLevel = call.smoothLevel * 0.72 + level * 0.28;
  el.callOrb.style.setProperty('--level', call.smoothLevel.toFixed(3));
  el.levelFill.style.width = `${Math.round(call.smoothLevel * 100)}%`;
  call.raf = requestAnimationFrame(loopLevels);
}

// ------------------------------------------------------------ 麦克风/画面

function updateMicButton() {
  const call = state.call;
  if (!call) return;
  el.micBtn.dataset.on = call.muted ? 'false' : 'true';
  el.micBtn.setAttribute('aria-pressed', call.muted ? 'false' : 'true');
}

function toggleMic() {
  const call = state.call;
  if (!call || !call.micActive) return;
  call.muted = !call.muted;
  call.mic.setEnabled(!call.muted);
  call.session.setMicEnabled(!call.muted);
  updateMicButton();
  toast(call.muted ? t('toast.micOff') : t('toast.micOn'), 'info', 1500);
}

async function toggleCamera() {
  const call = state.call;
  if (!call) return;
  if (call.camOn) {
    call.video.stop('camera');
    call.session.setVideoState('camera', false);
    call.camOn = false;
    el.cameraVideo.srcObject = null;
    updateVideoUI();
    toast(t('toast.camOff'), 'info', 1500);
    return;
  }
  try {
    await call.video.start('camera');
    if (!state.call) { call.video.stop('camera'); return; }
    call.camOn = true;
    el.cameraVideo.srcObject = call.video.getStream('camera');
    el.cameraVideo.play().catch(() => {});
    call.session.setVideoState('camera', true);
    updateVideoUI();
    toast(t('toast.camOn'), 'ok', 1500);
  } catch (err) {
    toast(err.message, 'error', 4200);
  }
}

async function toggleScreen() {
  const call = state.call;
  if (!call) return;
  if (call.screenOn) {
    call.video.stop('screen');
    call.session.setVideoState('screen', false);
    call.screenOn = false;
    el.screenVideo.srcObject = null;
    updateVideoUI();
    toast(t('toast.screenOff'), 'info', 1500);
    return;
  }
  try {
    await call.video.start('screen');
    if (!state.call) { call.video.stop('screen'); return; }
    call.screenOn = true;
    el.screenVideo.srcObject = call.video.getStream('screen');
    el.screenVideo.play().catch(() => {});
    call.session.setVideoState('screen', true);
    updateVideoUI();
    toast(t('toast.screenOn'), 'ok', 1500);
  } catch (err) {
    toast(err.message, 'error', 4200);
  }
}

function handleVideoEnded(source) {
  const call = state.call;
  if (!call) return;
  if (source === 'screen') {
    call.screenOn = false;
    el.screenVideo.srcObject = null;
    call.session.setVideoState('screen', false);
    toast(t('toast.screenOff'), 'info', 1500);
  } else {
    call.camOn = false;
    el.cameraVideo.srcObject = null;
    call.session.setVideoState('camera', false);
    toast(t('toast.camOff'), 'info', 1500);
  }
  updateVideoUI();
}

function updateVideoUI() {
  const call = state.call;
  if (!call) return;
  el.cameraTile.dataset.active = call.camOn ? 'true' : 'false';
  el.screenTile.dataset.active = call.screenOn ? 'true' : 'false';
  el.videoDock.hidden = !(call.camOn || call.screenOn);
  el.camBtn.dataset.on = call.camOn ? 'true' : 'false';
  el.camBtn.setAttribute('aria-pressed', call.camOn ? 'true' : 'false');
  el.screenBtn.dataset.on = call.screenOn ? 'true' : 'false';
  el.screenBtn.setAttribute('aria-pressed', call.screenOn ? 'true' : 'false');

  const both = call.camOn && call.screenOn;
  el.focusBtn.hidden = !both;
  if (both) {
    call.focus = call.focus === 'auto' ? 'auto' : call.focus;
    el.focusLabel.textContent = focusLabel(call.focus);
    call.session.setFocus(call.focus);
  }
}

function focusLabel(focus) {
  if (focus === 'camera') return t('focus.camera');
  if (focus === 'screen') return t('focus.screen');
  return t('focus.auto');
}

function cycleFocus() {
  const call = state.call;
  if (!call) return;
  const order = ['auto', 'camera', 'screen'];
  call.focus = order[(order.indexOf(call.focus) + 1) % order.length];
  call.session.setFocus(call.focus);
  el.focusLabel.textContent = focusLabel(call.focus);
  const key = call.focus === 'camera' ? 'toast.focusCamera' : call.focus === 'screen' ? 'toast.focusScreen' : 'toast.focusAuto';
  toast(t(key), 'info', 1500);
}

function resetVideoUI() {
  el.cameraTile.dataset.active = 'false';
  el.screenTile.dataset.active = 'false';
  el.videoDock.hidden = true;
  el.cameraVideo.srcObject = null;
  el.screenVideo.srcObject = null;
}

// ------------------------------------------------------------ 结束

async function endCall(reason = 'user') {
  const call = state.call;
  if (!call || call.ended) return;
  call.ended = true;
  state.call = null;

  clearInterval(call.timerId);
  cancelAnimationFrame(call.raf);
  closePanels();
  el.callOrb.style.setProperty('--level', '0');
  el.call.hidden = true;
  document.body.style.overflow = '';

  const duration = Date.now() - call.startedAt;
  const turns = call.turns;

  try { call.video.stopAll(); } catch (_) { /* noop */ }
  try { if (call.mic) await call.mic.close(); } catch (_) { /* noop */ }
  try { if (call.session) await call.session.close(); } catch (_) { /* noop */ }

  if (reason !== 'error') {
    toast(`${t('toast.callEnded')} · ${t('toast.summary', { dur: formatDuration(duration), turns })}`, 'ok', 3600);
  }
}

// ------------------------------------------------------------ 事件日志

function logDebug(dir, action, data) {
  const row = { dir, action, data, t: Date.now() };
  state.debug.rows.push(row);
  if (state.debug.rows.length > 400) state.debug.rows.shift();
  if (state.debug.open) appendDebugRow(row);
}

function appendDebugRow(row) {
  const node = document.createElement('div');
  node.className = 'dbg-row';
  node.dataset.dir = row.dir;
  const time = document.createElement('span');
  time.className = 'dbg-time';
  time.textContent = new Date(row.t).toLocaleTimeString([], { hour12: false });
  const body = document.createElement('span');
  body.className = 'dbg-act';
  const detail = row.data && Object.keys(row.data).length ? ` ${JSON.stringify(row.data).slice(0, 160)}` : '';
  body.textContent = `${row.dir === 'up' ? '↑' : row.dir === 'down' ? '↓' : row.dir === 'err' ? '!' : '·'} ${row.action}${detail}`;
  node.appendChild(time);
  node.appendChild(body);
  el.debugBody.appendChild(node);
  el.debugBody.scrollTop = el.debugBody.scrollHeight;
}

function toggleDebug(force) {
  const open = typeof force === 'boolean' ? force : el.debugPanel.hidden;
  el.debugPanel.hidden = !open;
  state.debug.open = open;
  if (open) {
    el.debugBody.innerHTML = '';
    state.debug.rows.slice(-200).forEach(appendDebugRow);
  }
}

// ------------------------------------------------------------ 面板

function closePanels() {
  el.transcriptPanel.hidden = true;
  el.transcriptBtn.setAttribute('aria-pressed', 'false');
  el.debugPanel.hidden = true;
  state.debug.open = false;
  el.settingsModal.hidden = true;
}

function toggleTranscript(force) {
  const open = typeof force === 'boolean' ? force : el.transcriptPanel.hidden;
  el.transcriptPanel.hidden = !open;
  el.transcriptBtn.setAttribute('aria-pressed', open ? 'true' : 'false');
  if (open) scrollTranscript();
}

function openSettings() {
  const persona = effectivePersona();
  const override = state.overrides[persona.id] || {};
  el.setName.value = persona.name;
  el.setPrompt.value = override.prompt ?? persona.base.prompt ?? '';
  el.setInstructions.value = override.instructions ?? persona.base.instructions ?? '';
  renderSettingsScenarios();
  el.settingsModal.hidden = false;
}

function renderSettingsScenarios() {
  el.setScenario.innerHTML = '';
  state.scenarios.forEach((scenario) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.dataset.scenario = scenario.id;
    chip.setAttribute('aria-pressed', scenario.id === state.scenarioId ? 'true' : 'false');
    chip.textContent = scenario.name;
    chip.addEventListener('click', () => {
      state.scenarioId = scenario.id;
      localStorage.setItem(LS.scenario, scenario.id);
      renderSettingsScenarios();
      renderScenarioChips();
    });
    el.setScenario.appendChild(chip);
  });
}

function savePersona() {
  const id = state.activePersonaId;
  const base = state.personas.find((p) => p.id === id) || {};
  const name = el.setName.value.trim() || base.name || 'Luna';
  const prompt = el.setPrompt.value.trim();
  const instructions = el.setInstructions.value.trim();
  const override = {};
  if (name !== base.name) override.name = name;
  if (prompt && prompt !== base.prompt) override.prompt = prompt;
  if (instructions && instructions !== base.instructions) override.instructions = instructions;
  if (Object.keys(override).length) state.overrides[id] = override;
  else delete state.overrides[id];
  saveJSON(LS.overrides, state.overrides);
  el.settingsModal.hidden = true;
  renderStage();
  toast(t('set.saved'), 'ok');
}

function resetPersona() {
  delete state.overrides[state.activePersonaId];
  saveJSON(LS.overrides, state.overrides);
  openSettings();
  renderStage();
}

// ------------------------------------------------------------ 绑定

function bindGlobalEvents() {
  el.langBtn.addEventListener('click', async () => {
    state.lang = getLang() === 'zh' ? 'en' : 'zh';
    localStorage.setItem(LS.lang, state.lang);
    setLang(state.lang);
    applyStaticI18n();
    el.langBtn.textContent = langButtonLabel();
    await loadConfig();
    checkHealth();
    if (state.call) {
      el.capAiTag.textContent = state.call.persona.name;
      setCallStatus(state.call.statusKey);
    }
  });

  el.transcriptBtn.addEventListener('click', () => toggleTranscript());
  el.closeTranscript.addEventListener('click', () => toggleTranscript(false));
  el.downloadBtn.addEventListener('click', exportTranscript);
  el.debugBtn.addEventListener('click', () => toggleDebug());
  el.closeDebug.addEventListener('click', () => toggleDebug(false));
  el.clearDebug.addEventListener('click', () => {
    state.debug.rows = [];
    el.debugBody.innerHTML = '';
  });

  el.micBtn.addEventListener('click', toggleMic);
  el.camBtn.addEventListener('click', toggleCamera);
  el.screenBtn.addEventListener('click', toggleScreen);
  el.focusBtn.addEventListener('click', cycleFocus);
  el.endBtn.addEventListener('click', () => endCall('user'));
  el.endBtnTop.addEventListener('click', () => endCall('user'));

  el.settingsBtn.addEventListener('click', openSettings);
  el.closeSettings.addEventListener('click', () => { el.settingsModal.hidden = true; });
  el.savePersona.addEventListener('click', savePersona);
  el.resetPersona.addEventListener('click', resetPersona);

  el.settingsModal.addEventListener('click', (event) => {
    if (event.target === el.settingsModal) el.settingsModal.hidden = true;
  });

  document.addEventListener('keydown', (event) => {
    const tag = (event.target && event.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (event.key === 'Escape') {
      if (!el.settingsModal.hidden) { el.settingsModal.hidden = true; return; }
      if (!el.transcriptPanel.hidden) { toggleTranscript(false); return; }
      if (!el.debugPanel.hidden) { toggleDebug(false); return; }
      if (state.call) {
        const now = Date.now();
        if (now - state.escArmed < 2500) {
          state.escArmed = 0;
          endCall('user');
        } else {
          state.escArmed = now;
          toast(t('toast.pressAgainEsc'), 'info', 2400);
        }
      }
      return;
    }
    if (!state.call) return;
    const key = event.key.toLowerCase();
    if (key === 'm') { event.preventDefault(); toggleMic(); }
    else if (key === 'c') { event.preventDefault(); toggleCamera(); }
    else if (key === 's') { event.preventDefault(); toggleScreen(); }
    else if (key === 'd') { event.preventDefault(); toggleDebug(); }
    else if (key === 't') { event.preventDefault(); toggleTranscript(); }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.call) {
      state.call.session.player.resume().catch(() => {});
    }
  });

  window.addEventListener('beforeunload', (event) => {
    if (!state.call) return;
    event.preventDefault();
    event.returnValue = '';
  });
}

boot();
