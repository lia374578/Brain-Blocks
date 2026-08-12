#!/usr/bin/env node
/* Patch Guess-the-number-voice.json (hand-made in the editor, not built by
   build_games.py) with extra contains-matchers, so kids' natural answers like
   "right", "up", "down", "high", "low", "more", "less" are accepted.

   Adds to the existing OR-chains:
     correct -> RIGHT
     lower   -> LOW, DOWN, LESS, SMALLER
     higher  -> HIGH, UP, MORE, BIGGER, LARGER

   Idempotent: a word already present in its branch is skipped. Rewrites the
   file with the editor's formatting (indent=2, trailing newline) and bumps the
   version to 0.4.1.

Usage: node tools/patch_gtn.mjs
*/
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FILE = path.join(__dirname, '..', 'Guess-the-number-voice.json');

const ADD = {
  CORRECT: ['RIGHT'],
  LOWER: ['LOW', 'DOWN', 'LESS', 'SMALLER'],
  HIGHER: ['HIGH', 'UP', 'MORE', 'BIGGER', 'LARGER'],
};

function collectFindTexts(b, out = []) {
  if (!b) return out;
  if (b.type === 'text_indexOf' && b.inputs && b.inputs.FIND) {
    const f = b.inputs.FIND.shadow || b.inputs.FIND.block;
    if (f && f.type === 'text') out.push(f.fields.TEXT);
  }
  if (b.inputs) for (const k in b.inputs) collectFindTexts(b.inputs[k].block, out);
  return out;
}

function walk(b, fn) {
  if (!b) return;
  fn(b);
  if (b.inputs) for (const k in b.inputs) walk(b.inputs[k].block, fn);
  if (b.next) walk(b.next.block, fn);
}

function leaf(findText) {
  return {
    type: 'logic_compare',
    fields: { OP: 'NEQ' },
    inputs: {
      A: {
        block: {
          type: 'text_indexOf',
          fields: { END: 'FIRST' },
          inputs: {
            VALUE: { block: { type: 'variables_get', fields: { VAR: { id: 'id_up' } } } },
            FIND: { shadow: { type: 'text', fields: { TEXT: findText } } },
          },
        },
      },
      B: { shadow: { type: 'math_number', fields: { NUM: 0 } } },
    },
  };
}

const data = JSON.parse(fs.readFileSync(FILE, 'utf8'));
const skill = data.skill || data;
const top = skill.blocks.blocks.blocks[0];

let patched = 0;
const branches = [];

walk(top, (b) => {
  if (b.type !== 'controls_if' || !b.inputs || !b.inputs.IF0 || !b.inputs.IF0.block) return;
  const texts = collectFindTexts(b.inputs.IF0.block);
  let kind = null;
  if (texts.includes('CORRECT')) kind = 'CORRECT';
  else if (texts.includes('TOO HIGH')) kind = 'LOWER';
  else if (texts.includes('TOO LOW')) kind = 'HIGHER';
  if (!kind) return;
  branches.push({ b, kind, texts });
});

for (const { b, kind, texts } of branches) {
  let cond = b.inputs.IF0.block;
  for (const word of ADD[kind]) {
    if (texts.includes(word)) continue; // idempotent
    cond = {
      type: 'logic_operation',
      fields: { OP: 'OR' },
      inputs: { A: { block: cond }, B: { block: leaf(word) } },
    };
    texts.push(word);
    patched++;
  }
  b.inputs.IF0.block = cond;
}

if (patched === 0) {
  console.log('no new matchers added (already patched?)');
} else {
  skill.version = '0.4.1';
  fs.writeFileSync(FILE, JSON.stringify(data, null, 2) + '\n', 'utf8');
  console.log('added %d matcher(s); wrote %s (version %s)', patched, FILE, skill.version);
}
