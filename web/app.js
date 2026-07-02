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


/* ===== terminal engine (was app.js) ===== */

/* Chrono terminal engine.
   Drives the whole session: prints Chrono's lines (with per-character
   typing sound + face animation), collects the user's answers through
   the single input line, and calls the Python bridge (app.py) stage by
   stage. Keeps text minimal but informative, per the design brief. */

const logEl = () => document.getElementById('log');
const cmdEl = () => document.getElementById('cmd');
const soundEl = () => document.getElementById('type-sound');

/* ---- sound: per-character | per-line | off, toggled from the header ---- */
const SOUND_MODES = ['per-char', 'per-line', 'off'];
let soundModeIdx = 0;
function soundMode() { return SOUND_MODES[soundModeIdx]; }

(function initSoundToggle() {
  const btn = document.getElementById('sound-toggle');
  const labels = { 'per-char': 'SOUND: PER-CHAR', 'per-line': 'SOUND: PER-LINE', 'off': 'SOUND: OFF' };
  function render() { btn.textContent = labels[soundMode()]; }
  btn.addEventListener('click', () => {
    soundModeIdx = (soundModeIdx + 1) % SOUND_MODES.length;
    render();
    // A tiny blip so the user hears what they picked.
    if (soundMode() !== 'off') blip();
  });
  render();
})();

function blip() {
  const a = soundEl();
  if (!a) return;
  try {
    const node = a.cloneNode(true);
    node.volume = 0.5;
    node.play().catch(() => {});
  } catch (e) { /* audio may be blocked until first interaction; ignore */ }
}

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

/* Type a line character-by-character, playing the typing sound per the
   current sound mode, and animating the face as "thinking" while typing. */
async function typeLine(tagText, tagClass, msgText, msgClass, opts = {}) {
  const speed = opts.speed || 12; // ms per char
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

  const mode = soundMode();
  if (mode === 'per-line') blip();

  for (let i = 0; i < msgText.length; i++) {
    msg.textContent += msgText[i];
    if (mode === 'per-char' && msgText[i] !== ' ') blip();
    _scroll();
    await wait(speed);
  }
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
  await typeLine('[2/5]', '', 'Task Analysis — classifying cognitive load + urgency...', 'muted');
  let analysis = await call('api_analyze_tasks');
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
  await typeLine('[3/5]', '', 'Optimization — running deterministic scheduler...', 'muted');
  const sched = await call('api_build_schedule');
  stopThinking();

  if (sched.capacity_error) {
    await typeLine('[3/5]', '', 'Could not fit everything once breaks are placed.', 'error');
    await typeLine(null, null, sched.capacity_error.message, 'muted');
    for (const s of sched.capacity_error.suggestions) {
      await typeLine(null, null, '  • ' + s, 'muted', { speed: 3 });
    }
    await typeLine('chrono', 'dim', 'Adjust your inputs and reload to try again.', 'muted');
    return;
  }
  if (sched.failed) {
    await typeLine('[3/5]', '', 'Scheduling failed: ' + (sched.validation_problems[0] || 'unknown'), 'error');
    return;
  }
  await typeLine('[3/5]', '', 'Schedule placed and validated.', 'success');

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
    startThinking();
    try {
      const cal = await call('api_write_calendar');
      stopThinking();
      await typeLine('[cal]', '', `Cleared ${cal.deleted} old event(s), wrote ${cal.created} new one(s).`, 'success');
      if (cal.calendar_link) addLinkChip('Open your Chrono calendar', cal.calendar_link);
    } catch (e) {
      stopThinking();
      await typeLine('[cal]', '', 'Calendar unavailable: ' + e.message, 'error');
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
    try {
      const pl = await call('api_write_planner');
      stopThinking();
      await typeLine('[xlsx]', '', `Planner ready — ${pl.written} entries.`, 'success');
      await typeLine(null, null, 'Saved to: ' + pl.path, 'muted');
    } catch (e) {
      stopThinking();
      await typeLine('[xlsx]', '', 'Planner error: ' + e.message, 'error');
    }
  } else {
    await typeLine('[xlsx]', '', 'Skipped — no planner file created.', 'muted');
  }

  addRule();
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

/* ---- kick off once DOM + eel are ready ---- */
window.addEventListener('DOMContentLoaded', async () => {
  // Show a face right away so the window is never blank while connecting.
  try { setFace(IDLE_FACE); } catch (e) {}

  // Unlock audio on first key/click (browsers block autoplay otherwise).
  const unlock = () => { blip(); window.removeEventListener('keydown', unlock); window.removeEventListener('click', unlock); };
  window.addEventListener('keydown', unlock);
  window.addEventListener('click', unlock);

  // Wait for the Python backend (eel) to connect before starting.
  addLineInstant('chrono', 'dim', 'Connecting to Chrono backend...', 'muted');
  const ready = await waitForEel();
  if (!ready) {
    addLineInstant('[error]', '', 'Could not reach the Python backend (eel.js not connected).', 'error');
    addLineInstant(null, null, 'Make sure you launched with "python app.py" (not by opening index.html directly).', 'muted');
    return;
  }
  logEl().innerHTML = '';
  runSession().catch(err => {
    stopThinking();
    addLineInstant('[fatal]', '', String(err && err.message || err), 'error');
    addLineInstant(null, null, 'Right-click → Inspect → Console for the full trace.', 'muted');
  });
});