#!/usr/bin/env node
/* Validate sum_remote.json (and gesture_remote_vision.json) against
   the REAL vendored Blockly 9.3.3 + the editor's custom zsibot blocks:
   1. load blockly browser-style in a vm context (headless Workspace),
   2. load the skill JSON exactly like the editor import does,
   3. codegen PER HAT like executeProgram (registerWhen hats first, then mains),
   4. execute with a mock api whose hear/gesture/human are scripted, using the
      REAL registerWhen/interrupt methods extracted from the HTML, and assert
      the expected motion/voice/gesture flows.

Usage: node tools/validate_gesture_voice_remote.mjs
*/
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const HTML = path.join(ROOT, 'brain_blocks_v2 1.html');

// ---------------------------------------------------------------------------
// Load Blockly browser-style into a vm context (same shims as validate_games.mjs)
// ---------------------------------------------------------------------------
function makeFakeDocument() {
  const mkEl = () => ({
    nodeType: 1, childNodes: [], children: [], attrs: {}, parentNode: null,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    setAttributeNS(_ns, k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.childNodes.push(c); this.children.push(c); c.parentNode = this; return c; },
    hasChildNodes() { return this.childNodes.length > 0; },
    hasAttributes() { return Object.keys(this.attrs).length > 0; },
    get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({ name, value })); },
    get firstChild() { return this.childNodes[0] || null; },
    get nextSibling() { return null; },
    removeChild(c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); return c; },
    cloneNode() { return mkEl(); },
    getElementsByTagName() { return []; },
    getAttribute(k) { return this.attrs[k]; },
    getAttributeNS(_ns, k) { return this.attrs[k]; },
    insertBefore(c, _ref) { this.appendChild(c); return c; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text || ''; },
  });
  return { createElementNS: () => mkEl(), createElement: () => mkEl(), documentElement: mkEl() };
}

function loadBlockly() {
  const ctx = vm.createContext({
    console, Math, Date, JSON, String, Number, Array, Object, RegExp,
    setTimeout, clearTimeout, setInterval, clearInterval,
    document: makeFakeDocument(),
    navigator: { userAgent: 'node' },
    location: { protocol: 'file:' },
  });
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'blockly/blockly.min.js'), 'utf8'), ctx);
  if (!ctx.Blockly) throw new Error('Blockly failed to load');
  return ctx;
}

function extractCustomBlocks(ctx) {
  const html = fs.readFileSync(HTML, 'utf8');
  const lines = html.split('\n');
  const start = lines.findIndex((l) => l.includes("Blockly.Blocks['zsibot_start']"));
  const end = lines.findIndex((l, i) => i > start && l.includes('Phone-side capabilities'));
  if (start < 0 || end < 0) throw new Error('could not locate custom block region');
  const region = lines.slice(start, end).join('\n');
  const actStart = lines.findIndex((l) => l.includes('const NAVI_STATIC_ACTIONS = ['));
  const actEnd = lines.findIndex((l, i) => i > actStart && l.includes('Custom Navi block definitions'));
  const actionRegion = 'var naviActionNames = {};\n' + lines.slice(actStart, actEnd).join('\n');
  vm.runInContext(actionRegion, ctx, { filename: 'editor-actions.js' });
  vm.runInContext(region, ctx, { filename: 'editor-custom-blocks.js' });
}

// Real registerWhen/interrupt from the HTML (robotApi's copy) — the mock uses
// the actual runtime semantics, not a reimplementation.
function extractWhenMethods() {
  const html = fs.readFileSync(HTML, 'utf8');
  const intStart = html.indexOf('registerWhen(condFn, fn) { this._whenList.push');
  const intEnd = html.indexOf('};', html.indexOf('async interrupt()')) + 2;
  return eval('({ ' + html.slice(intStart, intEnd - 2) + ' })');
}

// ---------------------------------------------------------------------------
// Load + per-hat codegen (mirrors executeProgram)
// ---------------------------------------------------------------------------
function loadSkill(ctx, jsonPath) {
  const raw = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  const skill = raw.skill || raw;
  if (!skill || String(skill.robot || '').toLowerCase() !== 'navi') throw new Error(jsonPath + ': not a navi skill');
  const Blockly = ctx.Blockly;
  const ws = new Blockly.Workspace();
  Blockly.serialization.workspaces.load(skill.blocks, ws);

  Blockly.JavaScript.init(ws);
  const all = ws.getTopBlocks(true);
  const defs = all.filter((b) => b.type === 'zsibot_def' || b.type === 'zsibot_def_return');
  const hats = all.filter((b) => !b.outputConnection && !b.previousConnection && b.type !== 'zsibot_def' && b.type !== 'zsibot_def_return');
  let defsCode = '';
  for (const d of defs) defsCode += (Blockly.JavaScript.blockToCode(d, false) || '') + '\n';
  const hatCodes = hats.map((b) => Blockly.JavaScript.blockToCode(b, b.type === 'zsibot_when') || '');
  const helperDefs = Blockly.JavaScript.finish('');
  return hats.map((b, i) => ({
    type: b.type,
    when: b.type === 'zsibot_when',
    code: defsCode + helperDefs + hatCodes[i],
  }));
}

// ---------------------------------------------------------------------------
// Mock api with scripted hear / gesture / human
// ---------------------------------------------------------------------------
function makeApi(opts) {
  const log = [];
  const whenMethods = extractWhenMethods();
  const api = Object.assign({
    _stopped: false, _startTime: 0,
    _whenList: [], _whenHandling: false,
    _opCount: 0, _opLimit: 4000,          // hard safety net
    _hearQ: (opts.hears || []).slice(),
    _lastHear: '',
    _stopPending: false,
    _didAction: false,
    _log: log,
    checkStop() {
      if (++this._opCount > this._opLimit) throw new Error('op limit — possible infinite loop');
      // scripted stop: end the run at the first boundary AFTER the Nth gesture
      // cycle has actually run its command
      if (this._stopPending && this._didAction) throw new Error('stopped');
      if (this._stopped) throw new Error('stopped');
    },
    highlight() {},
    speak: async (t) => log.push('SPEAK: ' + t),
    print: (t) => log.push('PRINT: ' + t),
    // background-STT stand-in: the when block's `new speech?` condition
    heardNew() {
      // defer while the when is latched (just fired) — real speech can't pile
      // up either, so don't deliver a second utterance into a latched window
      if (api._whenList[0] && api._whenList[0].fired) return false;
      if (!api._hearQ.length) return false;   // silence
      const u = api._hearQ.shift();
      if (!u) return false;
      api._lastHear = u;
      log.push('HEARD: ' + JSON.stringify(u));
      return true;
    },
    lastHear() { return api._lastHear; },
    gesture: async () => {
      const n = api._gestureN = (api._gestureN || 0) + 1;
      const r = opts.gestures ? opts.gestures[Math.min(n - 1, opts.gestures.length - 1)] : 'none';
      api._didAction = false;   // the command this cycle will set it again
      log.push('GESTURE: ' + JSON.stringify(r));
      if (opts.stopAfterGesture && n >= opts.stopAfterGesture) api._stopPending = true;
      return r || 'none';
    },
    human: async () => {
      const n = api._humanN = (api._humanN || 0) + 1;
      const r = opts.humans ? opts.humans[Math.min(n - 1, opts.humans.length - 1)] : { found: false };
      return r;
    },
    move: async (vx, vy, wz, dur) => { log.push(`MOVE vx=${vx} vy=${vy} wz=${wz} dur=${dur}`); api._didAction = true; },
    stand: async () => { log.push('STAND'); api._didAction = true; },
    lie: async () => { log.push('LIE'); api._didAction = true; },
    face: async () => { log.push('FACE'); api._didAction = true; },
    toward: async (sec) => { log.push('TOWARD ' + sec); api._didAction = true; },
    camera: async () => log.push('CAMERA'),
  }, whenMethods);
  return api;
}

async function runSkill(ctx, jsonPath, opts) {
  const hats = loadSkill(ctx, jsonPath);
  const api = makeApi(opts);
  // All hats in ONE shared function scope so variables coordinate across hats;
  // when hats first so they register before the main hats' first boundary
  // (mirrors executeProgram exactly).
  const allCode = hats.filter((h) => h.when).map((h) => h.code)
    .concat(hats.filter((h) => !h.when).map((h) => h.code))
    .join('\n');
  try {
    await new Function('api', 'return (async () => {\n' + allCode + '\n})();')(api);
  } catch (e) {
    if (!(e && e.message === 'stopped')) {
      console.log('   RUN ERROR:', e && e.message, '\n   LOG:\n   ' + api._log.slice(0, 80).join('\n   '));
      throw e;
    }   // scripted stop = normal end
  }
  return api;
}

// ---------------------------------------------------------------------------
let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok  ' + name);
  else { failures++; console.log('  FAIL ' + name + (detail ? ' — ' + detail : '')); }
}
const MOVES = (a) => a._log.filter((l) => l.startsWith('MOVE'));
const SPEAKS = (a) => a._log.filter((l) => l.startsWith('SPEAK: '));

// ---------------------------------------------------------------------------
const ctx = loadBlockly();
extractCustomBlocks(ctx);

const FILE = path.join(ROOT, 'sum_remote.json');

// --- 1. voice priority + max speeds -----------------------------------------
console.log('\n## voice priority + max speeds');
{
  const a = await runSkill(ctx, FILE, {
    hears: ['go forward', 'turn right'],
    gestures: ['palm'],          // must NOT fire while voice commands are heard
    stopAfterGesture: 1,
    humans: [{ found: false }],
  });
  const m = MOVES(a);
  check('voice "go forward" -> forward at max (vx=2, dur=1.5)', m[0] === 'MOVE vx=2 vy=0 wz=0 dur=1.5', m.join(' | '));
  check('voice "turn right" -> turn right at max (wz=-5, dur=0.5)', m[1] === 'MOVE vx=0 vy=0 wz=-5 dur=0.5', m.join(' | '));
  check('voice cycles produce only the 2 voice moves, then the silent cycle runs the gesture', m.length === 3 && m[2] === 'MOVE vx=2 vy=0 wz=0 dur=1.5', m.join(' | '));
  check('gesture called exactly once (only in the silent cycle)', a._log.filter((l) => l.startsWith('GESTURE')).length === 1);
  check('speaks the command', /Going forward!/.test(SPEAKS(a).join('')) && /Turning right!/.test(SPEAKS(a).join('')), SPEAKS(a).join(''));
}

// --- 2. mute user: every gesture works at max speed -------------------------
console.log('\n## gesture coverage (mute user)');
{
  const a = await runSkill(ctx, FILE, {
    hears: [],
    gestures: ['palm', 'thumbup', 'thumbdown', 'point-left', 'point-right', 'victory', 'love', 'fist', 'wave'],
    stopAfterGesture: 9,
    humans: [{ found: false }],
  });
  const m = MOVES(a);
  const got = a._log.filter((l) => /^(MOVE|STAND|LIE|FACE|TOWARD)/.test(l));
  const expected = [
    'MOVE vx=2 vy=0 wz=0 dur=1.5',   // palm -> forward max
    'STAND',                          // thumbup
    'MOVE vx=-2 vy=0 wz=0 dur=1.5',  // thumbdown -> backward max
    'MOVE vx=0 vy=0 wz=5 dur=0.5',   // point-left -> turn left max
    'MOVE vx=0 vy=0 wz=-5 dur=0.5',  // point-right -> turn right max
    'MOVE vx=0 vy=2 wz=0 dur=1',     // victory -> strafe left max
    'MOVE vx=0 vy=-2 wz=0 dur=1',    // love -> strafe right max
    'LIE',                            // fist
    'FACE',                           // wave -> come
  ];
  check('9 gestures -> 9 commands, in order', got.length === 9, got.join(' | '));
  if (got.some((l, i) => expected[i] !== l)) {
    console.log('   FULL LOG:\n   ' + a._log.join('\n   '));
  }
  expected.forEach((e, i) => check('gesture #' + (i + 1) + ' ' + JSON.stringify(e), got[i] === e, got[i]));
}

// --- 3. the when block IS the voice listener (`when new speech?`) ------------
console.log('\n## when [new speech] fires between blocks and overrides gestures');
{
  const a = await runSkill(ctx, FILE, {
    hears: ['go forward'],
    gestures: ['palm', 'palm'],
    stopAfterGesture: 1,
    humans: [{ found: false }],
  });
  const log = a._log;
  const hearIdx = log.findIndex((l) => l.startsWith('HEARD:'));
  const gestIdx = log.findIndex((l) => l.startsWith('GESTURE:'));
  const m = MOVES(a);
  check('when fires on new speech (1 HEARD)', log.filter((l) => l.startsWith('HEARD:')).length === 1, log.join(' | '));
  check('voice listener runs before gestures (HEARD before first GESTURE)', hearIdx >= 0 && gestIdx > hearIdx, log.join(' | '));
  check('voice overrides gestures that cycle (1 gesture call despite 2 cycles)', log.filter((l) => l.startsWith('GESTURE:')).length === 1, log.join(' | '));
  check('voice command executed from the when chain at max speed', m[0] === 'MOVE vx=2 vy=0 wz=0 dur=1.5', m.join(' | '));
  check('silent cycle runs the gesture', m[1] === 'MOVE vx=2 vy=0 wz=0 dur=1.5', m.join(' | '));
}

// --- 4. old skill still valid + new skill has no do_action -------------------
console.log('\n## gesture_remote_vision.json still loads/codegens');
{
  const oldHats = loadSkill(ctx, path.join(ROOT, 'gesture_remote_vision.json'));
  check('1 hat', oldHats.length === 1 && oldHats[0].type === 'zsibot_start', oldHats.map((h) => h.type).join(','));
  check('point-left/point-right branches present', /point-left/.test(oldHats[0].code) && /point-right/.test(oldHats[0].code));
  const newHats = loadSkill(ctx, FILE);
  check('new skill generated code never calls do_action', !newHats.some((h) => /api\.action\(/.test(h.code)));
  const a = await runSkill(ctx, FILE, {
    hears: ['forward'], gestures: ['point-left'], stopAfterGesture: 1, humans: [{ found: false }],
  });
  check('no ACTION calls at runtime', !a._log.some((l) => l.startsWith('ACTION')));
}

console.log(failures ? '\n' + failures + ' FAILURES' : '\nall checks passed');
process.exit(failures ? 1 : 0);
