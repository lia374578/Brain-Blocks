#!/usr/bin/env node
/* Validate Brain Blocks game JSONs against the REAL vendored Blockly 9.3.3:
   1. load blockly core + blocks + generators + msg + the editor's custom
      zsibot block definitions (extracted from the HTML) in a Node vm context,
      browser-style (no jsdom needed — headless Workspace),
   2. load each skill JSON exactly like the editor import does
      (Blockly.serialization.workspaces.load),
   3. generate the JavaScript (Blockly.JavaScript.workspaceToCode),
   4. execute it with a mock `api` and scripted player speech, asserting the
      expected game flow (win / taboo / repeat / give-up paths).

Usage: node tools/validate_games.mjs [game.json ...]
*/
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const HTML = path.join(ROOT, 'brain_blocks_v2 1.html');

// ---------------------------------------------------------------------------
// 1. Load Blockly browser-style into a vm context
// ---------------------------------------------------------------------------
function makeFakeDocument() {
  // Blockly's event system serializes blocks to XML for undo/redo listeners; a
  // headless workspace only needs inert XML-ish nodes (no real DOM required).
  const mkEl = () => ({
    nodeType: 1, childNodes: [], children: [], attrs: {}, parentNode: null,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    setAttributeNS(_ns, k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.childNodes.push(c); this.children.push(c); c.parentNode = this; return c; },
    hasChildNodes() { return this.childNodes.length > 0; },
    hasAttributes() { return Object.keys(this.attrs).length > 0; },
    get attributes() {
      const entries = Object.entries(this.attrs).map(([name, value]) => ({ name, value }));
      entries.item = (i) => entries[i] || null;
      return entries;
    },
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
  // blockly.min.js is the vendored concatenation: core + msg + blocks + JS generator.
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'blockly/blockly.min.js'), 'utf8'), ctx);
  if (!ctx.Blockly) throw new Error('Blockly failed to load in vm context');
  return ctx;
}

// Extract the editor's custom zsibot block definitions + generators from the HTML.
function extractCustomBlocks(ctx) {
  const html = fs.readFileSync(HTML, 'utf8');
  const lines = html.split('\n');
  const start = lines.findIndex((l) => l.includes("Blockly.Blocks['zsibot_start']"));
  const end = lines.findIndex((l, i) => i > start && l.includes('Phone-side capabilities'));
  if (start < 0 || end < 0) throw new Error('could not locate custom block region in HTML');
  const region = lines.slice(start, end).join('\n');
  // zsibot_action's dropdown menu generator calls currentActionOptions(); provide
  // the editor's static action list + an empty live-names map so it works offline.
  const actStart = lines.findIndex((l) => l.includes('const NAVI_STATIC_ACTIONS = ['));
  const actEnd = lines.findIndex((l, i) => i > actStart && l.includes('Custom Navi block definitions'));
  const actionRegion = 'var naviActionNames = {};\n' + lines.slice(actStart, actEnd).join('\n');
  try {
    vm.runInContext(actionRegion, ctx, { filename: 'editor-actions.js' });
    vm.runInContext(region, ctx, { filename: 'editor-custom-blocks.js' });
  } catch (e) {
    throw new Error('custom block extraction failed: ' + e.message);
  }
}

// ---------------------------------------------------------------------------
// 2. Load a skill JSON into a headless workspace + generate code
// ---------------------------------------------------------------------------
function loadSkill(ctx, jsonPath) {
  const raw = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  const skill = raw.skill || raw;
  if (!skill || String(skill.robot || '').toLowerCase() !== 'navi') {
    throw new Error(jsonPath + ': not a navi skill');
  }
  if (!skill.blocks || typeof skill.blocks !== 'object') {
    throw new Error(jsonPath + ': no blocks section');
  }
  const Blockly = ctx.Blockly;
  const ws = new Blockly.Workspace();
  const t0 = Date.now();
  Blockly.serialization.workspaces.load(skill.blocks, ws);
  const loadMs = Date.now() - t0;

  const top = ws.getTopBlocks(true);
  const hats = top.filter((b) => !b.outputConnection && !b.previousConnection);
  if (hats.length !== 1) throw new Error(jsonPath + ': expected exactly 1 hat, got ' + hats.length);

  // workspaceToCode = init + blockToCode(each top block) + finish — finish appends
  // the shared helper functions (mathRandomInt etc.) the same way the editor now does.
  const code = Blockly.JavaScript.workspaceToCode(ws);
  const blockCount = top.length + countSubBlocks(top);
  return { skill, hats, code, loadMs, blockCount };
}

function countSubBlocks(blocks) {
  let n = 0;
  const seen = new Set();
  const walk = (b) => {
    if (!b || seen.has(b.id)) return;
    seen.add(b.id);
    n++;
    for (const input of b.inputList) {
      if (input.connection && input.connection.targetBlock()) walk(input.connection.targetBlock());
    }
    if (b.nextConnection && b.nextConnection.targetBlock()) walk(b.nextConnection.targetBlock());
    for (const child of b.getChildren ? b.getChildren() : []) walk(child);
  };
  blocks.forEach(walk);
  return n - blocks.length;
}

// ---------------------------------------------------------------------------
// 3. Simulate with a mock api
// ---------------------------------------------------------------------------
function makeApi(scriptedHears) {
  const log = [];
  const api = {
    _queue: scriptedHears.slice(),
    _log: log,
    speak: async (text) => { log.push('SPEAK: ' + text); },
    hear: async (_pause, _max) => {
      if (!api._queue.length) throw new Error('scripted speech exhausted — the game kept listening');
      const r = api._queue.shift();
      log.push('HEAR => ' + JSON.stringify(r));
      return r;
    },
    playSound: (name) => { log.push('SOUND: ' + name); },
    print: (t) => { log.push('PRINT: ' + t); },
    action: async (id) => { log.push('ACTION: ' + id); },
    wait: async () => {},
    checkStop: async () => {},
    highlight: () => {},
    // boundary checks emitted by scrub_ since the when-interrupt change
    registerWhen: () => {},
    interrupt: async () => {},
  };
  return api;
}

function runCode(code, scriptedHears, mathRandom) {
  const api = makeApi(scriptedHears);
  const oldRand = Math.random;
  if (mathRandom !== undefined) Math.random = mathRandom;
  const fn = new Function('api', 'return (async () => {\n' + code + '\n})();');
  return fn(api).finally(() => { Math.random = oldRand; }).then(() => api);
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------
let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('  ok  ' + name);
  } else {
    failures++;
    console.log('  FAIL ' + name + (detail ? ' — ' + detail : ''));
  }
}
const SPEECH = (api) => api._log.filter((l) => l.startsWith('SPEAK: ')).map((l) => l.slice(7));
const ACTIONS = (api) => api._log.filter((l) => l.startsWith('ACTION: ')).map((l) => l.slice(8));
const ALL_LOG = (api) => api._log.join('\n');

async function testTwentyQuestions(ctx, file) {
  console.log('\n## ' + path.basename(file));
  const { code } = loadSkill(ctx, file);

  // win: lion (Y N Y N Y Y)
  {
    const api = await runCode(code, ['yes', 'no', 'yes', 'no', 'yes', 'yes']);
    const s = SPEECH(api).join(' | ');
    check('lion win', /It is a lion! I win!/i.test(s) && /Thanks for playing/.test(s), s);
    check('lion exactly one win', (s.match(/I win!/g) || []).length === 1, s);
    check('kick on win', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
  // win: owl (N Y N N Y Y)
  {
    const api = await runCode(code, ['no', 'yes', 'no', 'no', 'yes', 'yes']);
    const s = SPEECH(api).join(' | ');
    check('owl win', /It is an owl! I win!/i.test(s), s);
    check('kick on owl win', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
  // round 1 gibberish (never understood -> "?" answers -> no match),
  // round 2 correct (snake N N N N N Y) -> win on 2nd round
  {
    const garbage = Array(6).fill(['maybe', 'maybe', 'maybe', 'maybe']).flat();
    const snake = ['no', 'no', 'no', 'no', 'no', 'yes'];
    const api = await runCode(code, garbage.concat(snake));
    const s = SPEECH(api).join(' | ');
    check('retry round then snake win', /It is a snake! I win!/i.test(s), s);
    check('no give-up on retry win', !/I give up/.test(s), s);
  }
  // both rounds gibberish -> give up (loss)
  {
    const garbage = Array(12).fill(['maybe', 'maybe', 'maybe', 'maybe']).flat();
    const api = await runCode(code, garbage);
    const s = SPEECH(api).join(' | ');
    check('give up path', /I give up! You win this time/.test(s), s);
    check('kick on loss (give up)', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
  // win: dog (Y N N N Y Y)
  {
    const api = await runCode(code, ['yes', 'no', 'no', 'no', 'yes', 'yes']);
    const s = SPEECH(api).join(' | ');
    check('dog win', /It is a dog! I win!/i.test(s), s);
  }
  // win: duck (N Y N Y Y N)
  {
    const api = await runCode(code, ['no', 'yes', 'no', 'yes', 'yes', 'no']);
    const s = SPEECH(api).join(' | ');
    check('duck win', /It is a duck! I win!/i.test(s), s);
  }
  // "I don't know" must NOT count as NO (word-by-word matching) — retried
  {
    const api = await runCode(code, ["I don't know", 'yes', 'no', 'yes', 'no', 'yes', 'yes']);
    const s = SPEECH(api).join(' | ');
    check("don't know not NO", /It is a lion! I win!/i.test(s) && /I did not catch that/.test(s), s);
  }
  // "uh huh" counts as YES
  {
    const api = await runCode(code, ['uh huh', 'no', 'yes', 'no', 'yes', 'yes']);
    const s = SPEECH(api).join(' | ');
    check('uh huh is yes', /It is a lion! I win!/i.test(s), s);
  }
}

async function testTaboo(ctx, file) {
  console.log('\n## ' + path.basename(file));
  const { code } = loadSkill(ctx, file);

  // full win: each round says the target without any taboo word
  {
    const api = await runCode(code, [
      'bananas are sweet and curved',       // BANANA target
      'I play the guitar',                  // GUITAR target
      'arrr matey, I am a pirate',          // PIRATE target
      'the telescope is on the tripod',     // TELESCOPE target (no STARS/MOON/ZOOM)
      'a unicorn has a spiral mane',        // UNICORN target (no HORN/RAINBOW/MAGIC)
    ]);
    const s = SPEECH(api).join(' | ');
    check('5/5 win', /You got it! The word was BANANA/.test(s) && /out of 5\./.test(s) && /Taboo champion/.test(s), s);
    check('no false taboo in win run', !/Taboo! You said/.test(s), s);
    check('kick on each round win', ACTIONS(api).filter((a) => a === '516').length === 5,
      'actions: ' + ACTIONS(api).join(','));
  }
  // taboo detection (each round says exactly one taboo word)
  {
    const api = await runCode(code, [
      'yellow bananas',                 // YELLOW taboo
      'plucking guitar strings',        // STRING taboo + GUITAR target
      'arrr matey, buried treasure',    // TREASURE taboo
      'the moon through a telescope',   // MOON taboo + TELESCOPE target
      'horn and magic',                 // HORN taboo + UNICORN target
    ]);
    const s = SPEECH(api).join(' | ');
    check('taboo words caught', ['YELLOW', 'STRING', 'TREASURE', 'MOON', 'HORN'].every((w) =>
      new RegExp('Taboo! You said ' + w).test(s)), s);
    check('no points on taboo run', /You got 0 out of 5/.test(s), s);
    check('low-score message', /Good try!/.test(s), s);
    check('kick on each taboo (loss)', ACTIONS(api).filter((a) => a === '516').length === 5,
      'actions: ' + ACTIONS(api).join(','));
  }
  // silence -> time's up each round (neutral: no kick)
  {
    const api = await runCode(code, ['', '', '', '', '']);
    const s = SPEECH(api).join(' | ');
    check('time up x5', (s.match(/Time's up! The word was/g) || []).length === 5, s);
    check('final score', /You got 0 out of 5/.test(s), s);
    check('no kick on time-up', ACTIONS(api).length === 0, 'actions: ' + ACTIONS(api).join(','));
  }
  // word-boundary leniency: 'yellowish'/'sparkly' must NOT trip YELLOW/MAGIC
  {
    const api = await runCode(code, [
      'it is yellowish and curved like a banana',  // BANANA target
      'I play the guitar',                          // GUITAR target
      'arrr matey, I am a pirate',                  // PIRATE target
      'the telescope is on the tripod',             // TELESCOPE target (no STARS/MOON/ZOOM)
      'a unicorn has a sparkly mane',               // UNICORN target (no HORN/RAINBOW/MAGIC)
    ]);
    const s = SPEECH(api).join(' | ');
    check('lenient win 5/5', /out of 5\./.test(s) && /Taboo champion/.test(s), s);
    check('no taboo flags on lenient run', !/Taboo! You said/.test(s), s);
  }
  // plural taboo words still caught ('strings' trips STRING)
  {
    const api = await runCode(code, [
      'yellow bananas',            // round 1: YELLOW taboo
      'I pluck guitar strings',    // round 2: STRING taboo (plural form)
      'arrr matey',                // round 3: time's up
      'it is big',                 // round 4: time's up
      'it is sparkly',             // round 5: time's up
    ]);
    const s = SPEECH(api).join(' | ');
    check('plural taboo caught', /Taboo! You said STRING/.test(s), s);
  }
}

async function testCategories(ctx, file) {
  console.log('\n## ' + path.basename(file));
  const { code } = loadSkill(ctx, file);

  // Math.random = 0 -> category 1 (ANIMALS); win with repeats + one bogus word
  {
    const api = await runCode(code, [
      'dog', 'dog', 'spaceship', 'cat', 'fish', 'bird', 'lion',
    ], () => 0);
    const s = SPEECH(api).join(' | ');
    check('category is ANIMALS', /Your category is ANIMALS/.test(s), s);
    check('repeat caught', /You already said DOG!/.test(s), s);
    check('bogus word rejected', /not one I know/.test(s), s);
    check('win message', /You did it! Five ANIMALS!/.test(s), s);
    check('score reached 5', /That's 5 of 5/.test(s), s);
    check('kick on win', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
  // one multi-word utterance scores every valid word ('horse donkey pony' style)
  {
    const api = await runCode(code, ['cat dog fish bird lion'], () => 0);
    const s = SPEECH(api).join(' | ');
    check('multi-word single hear wins', /That's 5 of 5/.test(s) && /You did it! Five ANIMALS!/.test(s), s);
    check('no bogus message in multi-word win', !/not one I know/.test(s), s);
    check('kick on multi-word win', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
  // plurals + irregulars all count: rabbits, mice, horses, geese, sheep
  {
    const api = await runCode(code, ['rabbits', 'mice', 'horses', 'geese', 'sheep'], () => 0);
    const s = SPEECH(api).join(' | ');
    check('plurals accepted', /That's 5 of 5/.test(s) && /You did it! Five ANIMALS!/.test(s), s);
  }
  // empty hear prompts instead of a bogus rejection
  {
    const api = await runCode(code, ['', 'cat', 'dog', 'fish', 'bird', 'lion'], () => 0);
    const s = SPEECH(api).join(' | ');
    check('empty hear prompt', /I didn't hear you/.test(s), s);
    check('win after empty hear', /You did it! Five ANIMALS!/.test(s), s);
  }
  // word-boundary: 'pigeon' scores as PIGEON, never as PIG
  {
    const api = await runCode(code, ['pigeon', 'pig', 'cat', 'dog', 'fish', 'bird'], () => 0);
    const s = SPEECH(api).join(' | ');
    check('pigeon scored as itself', /That's 1 of 5/.test(s) && /That's 2 of 5/.test(s), s);
    check('no false repeat on pigeon/pig', !/already said PIG/.test(s), s);
  }
  // Math.random ~ 0.999 -> category 4 (SPORTS); all distinct
  {
    const api = await runCode(code, [
      'soccer', 'tennis', 'swimming', 'basketball', 'cycling',
    ], () => 0.999);
    const s = SPEECH(api).join(' | ');
    check('category is SPORTS', /Your category is SPORTS/.test(s), s);
    check('sports win', /You did it! Five SPORTS!/.test(s), s);
    check('no bogus on clean run', !/not one I know/.test(s), s);
  }
  // 5 repeated dogs (no points) + the other 5 ANIMALS items -> still reach 5
  {
    const api = await runCode(code,
      ['dog', 'dog', 'dog', 'dog', 'dog', 'cat', 'fish', 'bird', 'lion', 'bear'], () => 0);
    const s = SPEECH(api).join(' | ');
    check('win despite repeats', /You did it! Five ANIMALS!/.test(s), s);
    check('kick on win (repeats run)', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
}

async function testGuessNumber(ctx, file) {
  console.log('\n## ' + path.basename(file));
  const { code } = loadSkill(ctx, file);

  // player picks a number; Navi binary-searches; answer "correct" on the first guess
  {
    const api = await runCode(code, ['correct']);
    const s = SPEECH(api).join(' | ');
    check('guess win', /I got it! Your number was \d+ after 1 tries/.test(s), s);
    check('kick on guess win', ACTIONS(api).join(',') === '516', ACTIONS(api).join(','));
  }
  // "higher" then "correct" (guess 50 -> higher -> 75 -> correct)
  {
    const api = await runCode(code, ['higher', 'correct']);
    const s = SPEECH(api).join(' | ');
    check('guess win after higher', /I got it!/.test(s), s);
    check('kick once on guess win', ACTIONS(api).filter((a) => a === '516').length === 1,
      'actions: ' + ACTIONS(api).join(','));
  }
  // "right" counts as correct (kid says "that's right!")
  {
    const api = await runCode(code, ['right']);
    const s = SPEECH(api).join(' | ');
    check('right wins', /I got it! Your number was \d+ after 1 tries/.test(s), s);
  }
  // "up" counts as higher (kid says "up" instead of "higher")
  {
    const api = await runCode(code, ['up', 'correct']);
    const s = SPEECH(api).join(' | ');
    check('up means higher', /I got it!/.test(s), s);
  }
}

// ---------------------------------------------------------------------------
async function main() {
  const ctx = loadBlockly();
  extractCustomBlocks(ctx);
  const targets = process.argv.slice(2).length
    ? process.argv.slice(2)
    : ['20-Questions-voice.json', 'Taboo-voice.json', 'Categories-voice.json', 'Guess-the-number-voice.json'];

  for (const f of targets) {
    const file = path.join(ROOT, f);
    const { loadMs, blockCount } = loadSkill(ctx, file);
    console.log(`loaded ${f}: ${blockCount} blocks in ${loadMs}ms`);
  }
  // (games are re-loaded inside each test fn; keep the report simple)
  if (targets.some((t) => t.includes('20-Questions'))) await testTwentyQuestions(ctx, path.join(ROOT, targets.find((t) => t.includes('20-Questions'))));
  if (targets.some((t) => t.includes('Taboo'))) await testTaboo(ctx, path.join(ROOT, targets.find((t) => t.includes('Taboo'))));
  if (targets.some((t) => t.includes('Categories'))) await testCategories(ctx, path.join(ROOT, targets.find((t) => t.includes('Categories'))));
  if (targets.some((t) => t.includes('Guess-the-number'))) await testGuessNumber(ctx, path.join(ROOT, targets.find((t) => t.includes('Guess-the-number'))));

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nall checks passed');
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
