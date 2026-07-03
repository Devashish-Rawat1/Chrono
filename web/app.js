/* Chrono faces + matrix rain.
   Faces are taken verbatim from the user's expressions.txt:
   0 = idle (^ ^), 1 = <>, 2 = [], 3 = ** , 4 = thinking (*********). */

const FACES = [
`   ===================   
  =| --------------- |=  
 | |     ^    ^      | | 
  =| --------------- |=  
   ===================   `,
    `   ===================   
  =| --------------- |=  
 | |     <>   <>     | | 
  =| --------------- |=  
   ===================   `,
    `   ===================   
  =| --------------- |=  
 | |     []   []     | | 
  =| --------------- |=  
   ===================   `,
    `   ===================   
  =| --------------- |=  
 | |     **   **     | | 
  =| --------------- |=  
   ===================   `,
    `   ===================   
  =| --------------- |=  
 | | *************** | | 
  =| --------------- |=  
   ===================   `
];

const IDLE_FACE = 0;
const THINK_FACE = 4;
const _CYCLE = [1, 2, 3, 4, 1, 2, 3,4];

const _faceEl = () => document.getElementById('face');
let _cycleTimer = null;
let _cycleIdx = 0;

function setFace(i) {
  const el = _faceEl();
  if (el) el.textContent = FACES[i];
}

/* Start the "thinking/loading" eye animation (cycles <>, [], **). */
function startThinking() {
  stopThinking();
  _cycleIdx = 0;
  setFace(_CYCLE[0]);
  _cycleTimer = setInterval(() => {
    _cycleIdx = (_cycleIdx + 1) % _CYCLE.length;
    setFace(_CYCLE[_cycleIdx]);
  }, 260);
}

/* Stop thinking and settle on a final face (default: idle). */
function stopThinking(finalFace = IDLE_FACE) {
  if (_cycleTimer) clearInterval(_cycleTimer);
  _cycleTimer = null;
  setFace(finalFace);
}

/* ---- Matrix rain background (from the user's mock) ---- */
(function () {
  const canvas = document.getElementById('matrix-rain');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let width, height, columns, drops;
  const fontSize = 14;
  const chars = '01アイウエオカキクケコサシスセソタチツテトナニヌネノABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';

  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
    columns = Math.floor(width / fontSize);
    drops = new Array(columns).fill(0).map(() => Math.floor(Math.random() * -50));
  }
  window.addEventListener('resize', resize);
  resize();

  function draw() {
    ctx.fillStyle = 'rgba(5, 6, 5, 0.065)';
    ctx.fillRect(0, 0, width, height);
    ctx.font = fontSize + 'px monospace';
    for (let i = 0; i < columns; i++) {
      const char = chars[Math.floor(Math.random() * chars.length)];
      const x = i * fontSize;
      const y = drops[i] * fontSize;
      ctx.shadowColor = 'rgba(2, 232, 60, 0.8)';
      ctx.shadowBlur = 6;
      ctx.fillStyle = 'rgba(140, 255, 170, 0.9)';
      ctx.fillText(char, x, y);
      ctx.shadowBlur = 0;
      const trailChar = chars[Math.floor(Math.random() * chars.length)];
      ctx.fillStyle = 'rgba(2, 232, 60, 0.55)';
      ctx.fillText(trailChar, x, y - fontSize);
      if (y > height && Math.random() > 0.975) drops[i] = 0;
      drops[i]++;
    }
  }
  setInterval(draw, 45);
  setFace(IDLE_FACE);
})();


/* ===== amplitude visualizers flanking the face =====
   Adapted from the pixel-amplitude visualizer: two small pixel-bar
   canvases (left + right of the face) that animate while Chrono is
   "speaking" (i.e. writing text on screen). Compact by design so they
   fill the empty side space without dominating the header. */
(function () {
  const COLS = 9;      // few columns -> narrow
  const ROWS = 7;      // short
  const PIXEL = 8;
  const GAP = 2;
  const BAR_GAP = 3;
  const cellW = PIXEL + GAP;

  const canvases = [];
  let running = false;
  let t = 0;
  const phase = Array.from({ length: COLS * 2 }, () => Math.random() * Math.PI * 2);
  const speed = Array.from({ length: COLS * 2 }, () => 0.08 + Math.random() * 0.16);
  const smoothed = new Array(COLS * 2).fill(0);

  function setup(canvas) {
    if (!canvas) return null;
    const w = COLS * (cellW + BAR_GAP) - BAR_GAP - GAP;
    const h = ROWS * cellW - GAP;
    canvas.width = w;
    canvas.height = h;
    return canvas.getContext('2d');
  }

  function init() {
    for (const id of ['viz-left', 'viz-right']) {
      const c = document.getElementById(id);
      const ctx = setup(c);
      if (ctx) canvases.push({ el: c, ctx });
    }
  }

  function drawOne(ctx, w, h, offset) {
    ctx.clearRect(0, 0, w, h);
    for (let c = 0; c < COLS; c++) {
      // While speaking, lively bars; when settling, they fall to near-zero.
      const idx = offset + c;
      let target;
      if (running) {
        const base = Math.sin(t * speed[idx] + phase[idx]) * 0.5 + 0.5;
        target = Math.max(0.05, Math.min(1, base * 0.8 + Math.random() * 0.2));
      } else {
        target = 0.03;
      }
      smoothed[idx] += (target - smoothed[idx]) * 0.3;
      const litCount = Math.round(smoothed[idx] * ROWS);
      const x = c * (cellW + BAR_GAP);
      for (let r = 0; r < ROWS; r++) {
        const rowFromBottom = ROWS - 1 - r;
        const y = r * cellW;
        const isLit = rowFromBottom < litCount;
        if (isLit) {
          const heightRatio = rowFromBottom / ROWS;
          const alpha = Math.min(0.9, 0.35 + heightRatio * 0.65);
          ctx.fillStyle = `rgba(2,232,60,${alpha.toFixed(2)})`;
          ctx.shadowColor = 'rgba(2,232,60,0.8)';
          ctx.shadowBlur = 4;
        } else {
          ctx.fillStyle = 'rgba(2,232,60,0.06)';
          ctx.shadowBlur = 0;
        }
        ctx.fillRect(x, y, PIXEL, PIXEL);
      }
    }
  }

  function frame() {
    t += 1;
    // left canvas uses offset 0, right uses offset COLS (mirrored feel)
    if (canvases[0]) drawOne(canvases[0].ctx, canvases[0].el.width, canvases[0].el.height, 0);
    if (canvases[1]) drawOne(canvases[1].ctx, canvases[1].el.width, canvases[1].el.height, COLS);
    requestAnimationFrame(frame);
  }

  // Public hooks used by the terminal engine.
  window.vizStart = function () {
    running = true;
    canvases.forEach(c => c.el.classList.add('speaking'));
  };
  window.vizStop = function () {
    running = false;
    canvases.forEach(c => c.el.classList.remove('speaking'));
  };

  if (document.readyState !== 'loading') { init(); frame(); }
  else document.addEventListener('DOMContentLoaded', () => { init(); frame(); });
})();


/* ===== terminal engine (was app.js) ===== */

/* Chrono terminal engine.
   Drives the whole session: prints Chrono's lines (with per-character
   typing sound + face animation), collects the user's answers through
   the single input line, and calls the Python bridge (app.py) stage by
   stage. Keeps text minimal but informative, per the design brief. */

const logEl = () => document.getElementById('log');
const cmdEl = () => document.getElementById('cmd');

/* ============================================================
   AUDIO
   Two independent channels:
     1. Typing sound  — one short blip each time Chrono writes a line
        (irrespective of characters/lines). Always on; it's the UI's
        "writing" feedback.
     2. Chrono's VOICE — the spoken dialogue clips (online, analyzing,
        done, error, etc.). These are gated by a mute/unmute button so
        the user can silence Chrono's voice without killing the typing
        feedback.
   ============================================================ */

const TYPING_SOUND = 'assets/chrono-typing-sound.m4a';

/* Voice clips, keyed by intent. */
const VOICE = {
  online:        'assets/chrono-online.mp3',
  scheduleReady: 'assets/schedule-ready.mp3',
  timelineSync:  'assets/timeline-sync.mp3',
  mission:       'assets/mission-accomplished.mp3',
  analyzing:     'assets/analyzing.mp3',
  diagnostics:   'assets/diagnostics.mp3',
  scanning:      'assets/scanning.mp3',
  error:         'assets/error.mp3',
  mute:          'assets/mute.mp3',
  unmute:        'assets/unmute.mp3',
  // Hover easter eggs.
  master:        'assets/he-is-my-master.mp3',          // hover the Drazan badge
  mastersProfile:'assets/thats-my-masters-profile.mp3', // hover GitHub / LinkedIn
};

let voiceMuted = false;

/* ---- typing sound ----
   ONE reusable audio element that we start when a line begins typing and
   stop when it finishes. Reusing a single element (instead of spawning a
   new Audio per line) means the sound can't stack/overlap on itself when
   output is large -- it's a single continuous "typing" that starts and
   stops with the actual text appearing on screen. */
let _typingAudio = null;
let _typingActive = false;

function _ensureTypingAudio() {
  if (!_typingAudio) {
    _typingAudio = new Audio(TYPING_SOUND);
    _typingAudio.volume = 0.5;
    _typingAudio.loop = true;  // keep looping while a line types
  }
  return _typingAudio;
}

/* Begin the typing sound (idempotent). */
function typingSoundStart() {
  _typingActive = true;
  try {
    const a = _ensureTypingAudio();
    if (a.paused) { a.currentTime = 0; a.play().catch(() => {}); }
  } catch (e) {}
}

/* Stop the typing sound. Called when a line finishes (or output stops). */
function typingSoundStop() {
  _typingActive = false;
  try {
    if (_typingAudio && !_typingAudio.paused) {
      _typingAudio.pause();
      _typingAudio.currentTime = 0;
    }
  } catch (e) {}
}

/* ---- voice ----
   Voices must never overlap the typing sound. say() waits until typing is
   idle, then plays. Only one voice plays at a time; a new voice cancels a
   still-playing previous one so cues stay crisp.

   The amplitude visualizers flanking the face animate WHILE a voice clip
   is playing (they represent Chrono "speaking" out loud), and stop when
   the clip ends or is cut off. */
let _currentVoice = null;

function _vizOn() { if (window.vizStart) window.vizStart(); }
function _vizOff() { if (window.vizStop) window.vizStop(); }

/* Attach the visualizer to an audio element: on while it plays, off when
   it ends/pauses. */
function _bindViz(audio) {
  if (!audio) return;
  _vizOn();
  const off = () => _vizOff();
  audio.addEventListener('ended', off);
  audio.addEventListener('pause', off);
}

function _waitForTypingIdle(timeoutMs = 6000) {
  return new Promise((resolve) => {
    const start = Date.now();
    (function check() {
      if (!_typingActive || Date.now() - start > timeoutMs) return resolve();
      setTimeout(check, 60);
    })();
  });
}

/* Play a voice clip once typing has stopped. Returns a Promise that
   resolves with the Audio element (or null if muted/unavailable). */
async function say(key) {
  if (voiceMuted) return null;
  const src = VOICE[key];
  if (!src) return null;
  await _waitForTypingIdle();
  if (voiceMuted) return null;  // may have been muted while waiting
  try {
    if (_currentVoice) { try { _currentVoice.pause(); } catch (e) {} }
    const a = new Audio(src);
    a.volume = 0.9;
    _currentVoice = a;
    a.play().catch(() => {});
    _bindViz(a);   // animate the bars while this clip speaks
    return a;
  } catch (e) { return null; }
}

/* All currently-active sayOnLoop stoppers. Muting stops every one so a
   loop running mid-process (e.g. the calendar 'analyzing' loop) halts
   immediately rather than continuing to fire. */
const _activeLoops = new Set();

/* Repeatedly play a "working" voice clip with a 1s gap between plays,
   until stop() is called. Used for the long calendar/planner/analysis
   waits so Chrono keeps talking, but with a pause so it isn't annoying.
   Each repeat still waits for typing to be idle first. Returns stop(). */
function sayOnLoop(key) {
  if (voiceMuted) return () => {};
  let stopped = false;
  let current = null;
  let pendingTimer = null;

  async function cycle() {
    if (stopped || voiceMuted) { _vizOff(); return; }
    await _waitForTypingIdle();
    if (stopped || voiceMuted) { _vizOff(); return; }
    try {
      current = new Audio(VOICE[key]);
      current.volume = 0.9;
      if (_currentVoice) { try { _currentVoice.pause(); } catch (e) {} }
      _currentVoice = current;
      current.play().catch(() => {});
      _vizOn();
      current.addEventListener('ended', () => {
        if (stopped || voiceMuted) { _vizOff(); return; }
        _vizOff();
        pendingTimer = setTimeout(cycle, 1000);
      });
    } catch (e) { /* ignore */ }
  }
  cycle();

  const stop = function () {
    stopped = true;
    _vizOff();
    if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; }
    if (current) { try { current.pause(); current.currentTime = 0; } catch (e) {} }
    _activeLoops.delete(stop);
  };
  _activeLoops.add(stop);
  return stop;
}

/* Hard-stop ALL voice: any looping cues, the current one-shot clip, and
   the animation. Used by mute so nothing keeps talking or animating. */
function stopAllVoice() {
  for (const stop of Array.from(_activeLoops)) {
    try { stop(); } catch (e) {}
  }
  if (_currentVoice) { try { _currentVoice.pause(); _currentVoice.currentTime = 0; } catch (e) {} }
  _currentVoice = null;
  _vizOff();
}

/* ---- mute/unmute button ---- */
(function initVoiceToggle() {
  const btn = document.getElementById('sound-toggle');
  if (!btn) return;
  function render() { btn.textContent = voiceMuted ? 'CHRONO: MUTED' : 'CHRONO: SPEAKING'; }
  btn.addEventListener('click', () => {
    voiceMuted = !voiceMuted;
    render();
    if (voiceMuted) {
      // Kill everything currently speaking + the animation immediately.
      stopAllVoice();
      // Play the short "muted" confirmation (with its own viz), even
      // though voice is now muted -- it's the acknowledgement of the tap.
      try {
        const a = new Audio(VOICE.mute);
        a.volume = 0.9;
        a.play().catch(() => {});
        _vizOn();
        const off = () => _vizOff();
        a.addEventListener('ended', off);
        a.addEventListener('pause', off);
      } catch (e) {}
    } else {
      // Unmuting: the "can speak now" confirmation, with animation.
      say('unmute');
    }
  });
  render();
})();

/* ---- hover easter eggs ----
   Hovering the "Drazan" badge -> Chrono says "He is my master."
   Hovering the GitHub / LinkedIn icons -> "That's my master's profile."
   These respect mute (they're Chrono's voice) and animate the face bars
   like any other voice. A short re-trigger guard stops the clip from
   restarting on every tiny mouse move while the pointer sits on the
   element. */
(function initHoverVoices() {
  let hoverLock = false;

  function hoverSay(key) {
    if (voiceMuted || hoverLock) return;
    hoverLock = true;
    let released = false;
    const release = () => { if (!released) { released = true; hoverLock = false; } };
    // say() is async (it waits for typing to be idle), so it returns a
    // Promise that resolves to the Audio element.
    Promise.resolve(say(key)).then((a) => {
      if (a && a.addEventListener) a.addEventListener('ended', release);
    });
    setTimeout(release, 4000);  // fallback release
  }

  const badge = document.getElementById('credit-badge');
  if (badge) {
    badge.style.pointerEvents = 'auto';  // badge is normally non-interactive
    badge.addEventListener('mouseenter', () => hoverSay('master'));
  }

  // Both social links share the "master's profile" clip.
  document.querySelectorAll('#social-icons a').forEach((el) => {
    el.addEventListener('mouseenter', () => hoverSay('mastersProfile'));
  });
})();

/* ---- low-level printing ---- */
function wait(ms) { return new Promise(r => setTimeout(r, ms)); }

function _scroll() { const l = logEl(); l.scrollTop = l.scrollHeight; }

function addRule() {
  const r = document.createElement('div');
  r.className = 'rule';
  logEl().appendChild(r);
  _scroll();
}

/* Instantly add a line (no typing) — used for the user's own echoed input
   and for structured/preformatted blocks like the schedule. */
function addLineInstant(tagText, tagClass, msgText, msgClass) {
  const line = document.createElement('div');
  line.className = 'line';
  if (tagText !== null) {
    const tag = document.createElement('span');
    tag.className = 'tag ' + (tagClass || '');
    tag.textContent = tagText;
    line.appendChild(tag);
  }
  const msg = document.createElement('span');
  msg.className = 'msg ' + (msgClass || '');
  msg.textContent = msgText;
  line.appendChild(msg);
  logEl().appendChild(line);
  _scroll();
  return msg;
}

/* Type a line character-by-character. One typing blip plays per line
   (irrespective of length), and the face animates while typing. */
async function typeLine(tagText, tagClass, msgText, msgClass, opts = {}) {
  const speed = opts.speed || 18; // ms per char (slightly relaxed pace)
  const line = document.createElement('div');
  line.className = 'line';
  line.style.opacity = 1;
  if (tagText !== null) {
    const tag = document.createElement('span');
    tag.className = 'tag ' + (tagClass || '');
    tag.textContent = tagText;
    line.appendChild(tag);
  }
  const msg = document.createElement('span');
  msg.className = 'msg ' + (msgClass || '');
  line.appendChild(msg);
  logEl().appendChild(line);

  // Typing sound plays WHILE the characters appear, and stops the moment
  // the line finishes -- timed to the on-screen output.
  typingSoundStart();
  for (let i = 0; i < msgText.length; i++) {
    msg.textContent += msgText[i];
    _scroll();
    await wait(speed);
  }
  typingSoundStop();
  return msg;
}

/* Print a multi-line block (e.g. the schedule or the explanation),
   optionally typed. Preserves newlines. */
async function typeBlock(text, msgClass, opts = {}) {
  const lines = text.split('\n');
  for (const ln of lines) {
    if (opts.instant) addLineInstant(null, null, ln, msgClass);
    else await typeLine(null, null, ln, msgClass, opts);
  }
}

/* ---- input handling: a promise that resolves on Enter ---- */
let _inputResolver = null;
cmdEl().addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && _inputResolver) {
    const val = cmdEl().value;
    cmdEl().value = '';
    const r = _inputResolver;
    _inputResolver = null;
    r(val);
  }
});

/* Ask one question, echo the answer back into the log, return the text. */
function ask() {
  return new Promise((resolve) => {
    cmdEl().disabled = false;
    cmdEl().focus();
    _inputResolver = (val) => {
      addLineInstant('>', 'dim', val, 'user');
      resolve(val);
    };
  });
}

/* Collect a multi-line answer: keep asking until a blank line. */
async function askMultiline() {
  const lines = [];
  while (true) {
    const v = await ask();
    if (v.trim() === '') break;
    lines.push(v);
  }
  return lines.join('\n');
}

/* ---- backend bridge helper ---- */

/* Wait until eel has loaded and connected. eel.js defines window.eel and
   attaches the exposed functions once the websocket is up; calling too
   early throws "eel is not defined" or "eel.api_x is not a function". */
async function waitForEel(timeoutMs = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (typeof window.eel !== 'undefined' && typeof window.eel.api_backend_name === 'function') {
      return true;
    }
    await wait(100);
  }
  return false;
}

async function call(fn, ...args) {
  if (typeof window.eel === 'undefined' || typeof window.eel[fn] !== 'function') {
    throw new Error(`backend not connected (eel.${fn} unavailable)`);
  }
  // eel exposes async wrappers returning our {ok, data|error} envelope.
  const res = await window.eel[fn](...args)();
  if (!res || res.ok === false) {
    throw new Error((res && res.error) || 'unknown backend error');
  }
  return res.data;
}

/* ---- the session flow ---- */
async function runSession() {
  cmdEl().disabled = true;
  setFace(IDLE_FACE);

  // Beat of silence after boot, then the "Chrono is online" greeting.
  await wait(2000);
  const online = say('online');
  // Give the greeting a moment before Chrono starts typing, so the voice
  // and the typing sound don't collide (voice first, then typing).
  await wait(1400);

  let backend = 'groq';
  try { backend = await call('api_backend_name'); } catch (e) {}

  await typeLine('chrono', 'dim', 'Chrono — An Autonomous Planning Agent', 'muted');
  await typeLine('chrono', 'dim', `Let's set up your week.`, 'muted');
  addRule();

  // ---- Onboarding questions ----
  await typeLine('[Q]', '', 'What time do you usually wake up and go to sleep?  (e.g. 7:00 AM - 11:00 PM)', 'muted');
  const wake_sleep = await ask();

  await typeLine('[Q]', '', 'Any fixed weekly commitments? One per line, blank line when done.', 'muted');
  await typeLine(null, null, '    e.g. Gym: Mon/Wed/Fri 4 PM - 5 PM', 'muted', { speed: 4 });
  const recurring = await askMultiline();

  await typeLine('[Q]', '', 'How many hours can you focus continuously before a break?  (e.g. 2)', 'muted');
  const focus = await ask();

  await typeLine('[Q]', '', 'What tasks/goals to schedule this week? One per line, blank line when done.', 'muted');
  await typeLine(null, null, '    e.g. DSA Practice (2 hrs/day)   |   Build Chrono (10 hrs/week)', 'muted', { speed: 4 });
  const tasks = await askMultiline();

  cmdEl().disabled = true;
  addRule();

  const answers = {
    wake_sleep,
    recurring_commitments: recurring,
    focus_span: focus,
    tasks,
  };

  // ---- Stage 1: onboarding ----
  startThinking();
  await typeLine('[1/5]', '', 'Onboarding — structuring your inputs...', 'muted');
  const onb = await call('api_process_onboarding', answers);
  stopThinking();
  await typeLine('[1/5]', '', `Onboarding done — ${onb.tasks.length} task(s), window ${onb.window.wake} to ${onb.window.sleep}.`, 'success');

  // ---- Stage 2: task analysis (+ clarifications) ----
  startThinking();
  const stopAnalyzing = sayOnLoop('analyzing');
  await typeLine('[2/5]', '', 'Task Analysis — classifying cognitive load + urgency...', 'muted');
  let analysis = await call('api_analyze_tasks');
  stopAnalyzing();
  stopThinking();
  await typeLine('[2/5]', '', `Groq classified ${analysis.count} task(s).`, 'success');

  if (analysis.needs_clarification && analysis.needs_clarification.length) {
    await typeLine('[?]', '', 'A couple of tasks need clarification:', 'muted');
    const clar = {};
    for (const item of analysis.needs_clarification) {
      await typeLine(null, null, `    ${item.question}`, 'muted');
      clar[item.name] = await ask();
    }
    cmdEl().disabled = true;
    startThinking();
    await call('api_apply_clarifications', clar);
    stopThinking();
  }

  // ---- Stage 3: build schedule ----
  startThinking();
  const stopSched = sayOnLoop('diagnostics');
  await typeLine('[3/5]', '', 'Optimization — running deterministic scheduler...', 'muted');
  const sched = await call('api_build_schedule');
  stopSched();
  stopThinking();

  if (sched.capacity_error) {
    say('error');
    await typeLine('[3/5]', '', 'Could not fit everything once breaks are placed.', 'error');
    await typeLine(null, null, sched.capacity_error.message, 'muted');
    for (const s of sched.capacity_error.suggestions) {
      await typeLine(null, null, '  • ' + s, 'muted', { speed: 3 });
    }
    await typeLine('chrono', 'dim', 'Adjust your inputs and reload to try again.', 'muted');
    return;
  }
  if (sched.failed) {
    say('error');
    await typeLine('[3/5]', '', 'Scheduling failed: ' + (sched.validation_problems[0] || 'unknown'), 'error');
    return;
  }
  await typeLine('[3/5]', '', 'Schedule placed and validated.', 'success');
  say('scheduleReady');

  // ---- Print the schedule grouped by day, with a Copy chip ----
  const scheduleText = formatSchedule(sched.blocks);
  addRule();
  await typeBlock(scheduleText, 'muted', { instant: true });
  addCopyChip(scheduleText);
  addRule();

  // ---- Stage 4: explanation ----
  startThinking();
  await typeLine('[4/5]', '', 'Explanation — summarizing your week...', 'muted');
  const exp = await call('api_explain_schedule');
  stopThinking();
  await typeLine('[4/5]', '', 'Here\'s your week:', 'success');
  if (exp.summary) await typeBlock('\n' + exp.summary + '\n', 'prose', { speed: 6 });

  // ---- Stage 5: calendar + planner (each gated on a y/n) ----
  addRule();
  await typeLine('[5/5]', '', 'Publish this schedule to your Google Calendar? (y/n)', 'muted');
  const wantCal = (await ask()).trim().toLowerCase();
  if (wantCal === 'y' || wantCal === 'yes') {
    cmdEl().disabled = true;
    await typeLine('[cal]', '', 'Writing events to Google Calendar — this can take a moment...', 'muted');
    startThinking();
    const stopScan = sayOnLoop('analyzing');  // longest wait: keep Chrono talking
    try {
      const cal = await call('api_write_calendar');
      stopScan();
      stopThinking();
      await typeLine('[cal]', '', `Cleared ${cal.deleted} old event(s), wrote ${cal.created} new one(s).`, 'success');
      say('timelineSync');
      if (cal.calendar_link) addLinkChip('Open your Chrono calendar', cal.calendar_link);
    } catch (e) {
      stopScan();
      stopThinking();
      await typeLine('[cal]', '', 'Calendar unavailable: ' + e.message, 'error');
      say('error');
      await typeLine(null, null, 'If your Google login expired, delete config/token.json and reload.', 'muted');
    }
  } else {
    await typeLine('[cal]', '', 'Skipped — nothing written to your calendar.', 'muted');
  }

  await typeLine('[5/5]', '', 'Generate a downloadable Excel planner? (y/n)', 'muted');
  const wantXls = (await ask()).trim().toLowerCase();
  if (wantXls === 'y' || wantXls === 'yes') {
    cmdEl().disabled = true;
    startThinking();
    const stopScan2 = sayOnLoop('scanning');
    try {
      const pl = await call('api_write_planner');
      stopScan2();
      stopThinking();
      await typeLine('[xlsx]', '', `Planner ready — ${pl.written} entries.`, 'success');
      addDownloadChip('Download your Chrono planner');
    } catch (e) {
      stopScan2();
      stopThinking();
      say('error');
      await typeLine('[xlsx]', '', 'Planner error: ' + e.message, 'error');
    }
  } else {
    await typeLine('[xlsx]', '', 'Skipped — no planner file created.', 'muted');
  }

  addRule();
  say('mission');  // "Mission accomplished"
  await typeLine('chrono', 'dim', 'All done. Reload to plan another week.', 'muted');
  setFace(IDLE_FACE);
}

/* ---- helpers for schedule formatting + action chips ---- */
function to12h(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  const period = h < 12 ? 'AM' : 'PM';
  const hh = (h % 12) || 12;
  return `${hh}:${String(m).padStart(2, '0')} ${period}`;
}

function formatSchedule(blocks) {
  const DAYS = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const byDay = {};
  for (const b of blocks) (byDay[b.day] = byDay[b.day] || []).push(b);
  const out = [];
  for (const d of DAYS) {
    if (!byDay[d]) continue;
    out.push(d);
    byDay[d].sort((a, b) => a.start.localeCompare(b.start));
    for (const b of byDay[d]) {
      const label = b.label.padEnd(14);
      out.push(`  ${to12h(b.start)} – ${to12h(b.end)}   ${label} (${b.type})`);
    }
    out.push('');
  }
  return out.join('\n').trimEnd();
}

function addCopyChip(text) {
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.textContent = '[ Copy schedule ]';
  chip.onclick = () => {
    navigator.clipboard.writeText(text).then(() => {
      chip.textContent = '[ Copied ✓ ]';
      setTimeout(() => (chip.textContent = '[ Copy schedule ]'), 1500);
    });
  };
  logEl().appendChild(chip);
  _scroll();
}

function addLinkChip(label, url) {
  const a = document.createElement('a');
  a.className = 'chip';
  a.textContent = '[ ' + label + ' ]';
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  logEl().appendChild(a);
  _scroll();
}

/* A download chip: on click, asks the backend for the planner file as
   base64, then triggers a real browser download so the user gets the
   .xlsx without hunting for it on disk. */
function addDownloadChip(label) {
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.textContent = '[ ' + label + ' ]';
  chip.onclick = async () => {
    const original = chip.textContent;
    chip.textContent = '[ Preparing download... ]';
    try {
      const dl = await call('api_get_planner_download');
      // Rebuild the binary from base64 and save it via a temporary link.
      const bytes = Uint8Array.from(atob(dl.b64), c => c.charCodeAt(0));
      const blob = new Blob([bytes], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = dl.filename || 'Chrono-Weekly-Planner.xlsx';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      chip.textContent = '[ Downloaded ✓ ]';
      setTimeout(() => { chip.textContent = original; }, 1800);
    } catch (e) {
      chip.textContent = '[ Download failed ]';
      setTimeout(() => { chip.textContent = original; }, 1800);
    }
  };
  logEl().appendChild(chip);
  _scroll();
}

/* ---- kick off once DOM + eel are ready ---- */

/* Show a "click to begin" gate. Browsers block audio until the user
   interacts with the page, so the very first boot (with "Chrono is
   online" + the typing sound) would otherwise be silent. Requiring one
   click to start guarantees audio is unlocked before anything plays. */
function showStartGate() {
  return new Promise((resolve) => {
    const gate = document.createElement('div');
    gate.id = 'start-gate';
    gate.innerHTML = `
      <div class="gate-inner">
        <div class="gate-logo-wrap">
          <img src="assets/chrono-icon.png" alt="Chrono" class="gate-logo">
          <div class="gate-scan"></div>
        </div>
        <div class="gate-title">CHRONO</div>
        <div class="gate-sub">AN AUTONOMOUS PLANNING AGENT</div>
        <div class="gate-cta">▶ CLICK ANYWHERE TO BOOT</div>
      </div>`;
    document.body.appendChild(gate);
    const begin = () => {
      // Prime the typing audio within the user gesture so later plays work.
      try {
        const a = _ensureTypingAudio();
        a.play().then(() => { a.pause(); a.currentTime = 0; }).catch(() => {});
      } catch (e) {}
      gate.removeEventListener('click', begin);
      window.removeEventListener('keydown', begin);
      gate.remove();
      resolve();
    };
    gate.addEventListener('click', begin);
    window.addEventListener('keydown', begin);
  });
}

window.addEventListener('DOMContentLoaded', async () => {
  // Show a face right away so the window is never blank while connecting.
  try { setFace(IDLE_FACE); } catch (e) {}

  // Wait for the Python backend (eel) to connect.
  addLineInstant('chrono', 'dim', 'Connecting to Chrono backend...', 'muted');
  const ready = await waitForEel();
  if (!ready) {
    addLineInstant('[error]', '', 'Could not reach the Python backend (eel.js not connected).', 'error');
    addLineInstant(null, null, 'Make sure you launched with "python app.py" (not by opening index.html directly).', 'muted');
    return;
  }
  logEl().innerHTML = '';

  // Gate on a click so audio is unlocked, THEN run the session.
  await showStartGate();

  runSession().catch(err => {
    stopThinking();
    addLineInstant('[fatal]', '', String(err && err.message || err), 'error');
    addLineInstant(null, null, 'Right-click → Inspect → Console for the full trace.', 'muted');
  });
});