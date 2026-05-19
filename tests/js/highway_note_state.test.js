// Source-level guards for the per-note judgment hook (slopsmith#254):
// highway.setNoteStateProvider / getNoteStateProvider / getNoteState,
// bundle.getNoteState, isDefaultRenderer, and the _noteState
// normalization rules. The createHighway closure owns canvas + WebGL
// lifecycle that's too heavy for a vm sandbox, so — like the other
// highway tests in this dir — these lock in the wiring by inspecting
// the source rather than executing it.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const highwayJs = path.join(__dirname, '..', '..', 'static', 'highway.js');
const highway3dJs = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');

// Brace-balanced extraction (same helper shape as highway_visibility.test.js).
function extractBlock(src, signature) {
    const start = src.indexOf(signature);
    assert.ok(start !== -1, `signature '${signature}' not found`);
    const openBrace = src.indexOf('{', start);
    assert.ok(openBrace !== -1, `opening brace after '${signature}' not found`);
    let depth = 1;
    let i = openBrace + 1;
    while (i < src.length && depth > 0) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') depth--;
        i++;
    }
    assert.ok(depth === 0, `unbalanced braces after '${signature}'`);
    return src.slice(start, i);
}

test('highway declares the note-state provider slot', () => {
    const src = fs.readFileSync(highwayJs, 'utf8');
    assert.match(src, /let\s+_noteStateProvider\s*=\s*null/, 'missing _noteStateProvider (provider slot, null = none)');
});

test('public API exposes setNoteStateProvider / getNoteStateProvider / getNoteState / isDefaultRenderer', () => {
    const src = fs.readFileSync(highwayJs, 'utf8');
    assert.match(src, /setNoteStateProvider\s*\(\s*fn\s*\)\s*\{[^}]*_noteStateProvider\s*=/, 'setNoteStateProvider must assign _noteStateProvider');
    assert.match(src, /setNoteStateProvider\s*\(\s*fn\s*\)\s*\{[^}]*typeof\s+fn\s*===\s*['"]function['"][^}]*:\s*null/, 'setNoteStateProvider must coerce non-functions (incl. null) to null');
    assert.match(src, /getNoteStateProvider\s*\(\s*\)\s*\{\s*return\s+_noteStateProvider/, 'getNoteStateProvider must return the slot');
    assert.match(src, /getNoteState\s*\(\s*note\s*,\s*chartTime\s*\)\s*\{\s*return\s+_noteState\s*\(/, 'getNoteState must delegate to _noteState');
    assert.match(src, /isDefaultRenderer\s*\(\s*\)\s*\{\s*return\s+_renderer\s*===\s*_defaultRenderer\s*\|\|\s*_renderer\s*==\s*null/, 'isDefaultRenderer must be (_renderer === _defaultRenderer || _renderer == null)');
});

test('_makeBundle exposes getNoteState (stable reference, no per-frame alloc)', () => {
    const src = fs.readFileSync(highwayJs, 'utf8');
    const fn = extractBlock(src, 'function _makeBundle()');
    // The bundle field must point straight at _noteState — not a fresh
    // arrow each frame (the per-frame allocation the review flagged).
    assert.match(fn, /getNoteState:\s*_noteState\b/, 'bundle.getNoteState must be the stable _noteState reference');
});

test('_noteState normalizes provider output as documented', () => {
    const src = fs.readFileSync(highwayJs, 'utf8');
    const fn = extractBlock(src, 'function _noteState(note, chartTime)');
    assert.match(fn, /if\s*\(\s*!_noteStateProvider\s*\)\s*return\s+null/, 'must short-circuit when no provider is registered');
    assert.match(fn, /try\s*\{[\s\S]*_noteStateProvider\s*\([\s\S]*catch[\s\S]*return\s+null/, 'must call the provider inside try/catch and return null on throw');
    assert.match(fn, /state\s*!==\s*['"]hit['"]\s*&&\s*state\s*!==\s*['"]active['"]\s*&&\s*state\s*!==\s*['"]miss['"]/, 'must reject states other than hit/active/miss');
    assert.match(fn, /Math\.max\(\s*0\s*,\s*Math\.min\(\s*1\s*,\s*raw\.alpha\s*\)\s*\)/, 'must clamp alpha to [0,1]');
    assert.match(fn, /if\s*\(\s*alpha\s*<=\s*0\s*\)\s*return\s+null/, 'must return null when alpha resolves to <= 0');
    assert.match(fn, /return\s*\{\s*state\s*,\s*alpha\s*,\s*color\s*\}/, 'must return the normalized { state, alpha, color }');
});

test('default 2D renderer threads note state into drawNote / drawSustains / chord path', () => {
    const src = fs.readFileSync(highwayJs, 'utf8');
    // drawNote takes the trailing `ns` param.
    assert.match(src, /function\s+drawNote\(\s*W\s*,\s*H\s*,\s*x\s*,\s*y\s*,\s*scale\s*,\s*string\s*,\s*fret\s*,\s*opts\s*,\s*ns\s*\)/, 'drawNote must accept the trailing ns param');
    // drawNotes / drawSustains / drawChords gate the lookup on the provider.
    assert.match(src, /_noteStateProvider\s*\?\s*_noteState\(\s*n\s*,\s*n\.t\s*\)\s*:\s*null/, 'visible-note paths must skip the lookup when no provider is set');
    assert.match(src, /_noteStateProvider\s*\?\s*_noteState\(\s*cn\s*,\s*ch\.t\s*\)\s*:\s*null/, 'chord-note path must key the lookup by the chord time and gate on the provider');
});

test('3D highway captures bundle.getNoteState and overrides legacy hit/miss with the provider verdict', () => {
    const src = fs.readFileSync(highway3dJs, 'utf8');
    assert.match(src, /_ndGetNoteState\s*=\s*\(bundle\s*&&\s*typeof\s+bundle\.getNoteState\s*===\s*['"]function['"]\)\s*\?\s*bundle\.getNoteState\s*:\s*null/, 'update() must capture bundle.getNoteState into _ndGetNoteState');
    // Provider verdict wins: miss => not _showHit; otherwise provider state
    // or the legacy fallback (`hit`) plus the pre-hit ghost window preview.
    assert.match(src, /const\s+_showHit\s*=\s*\(\s*_ndState\s*===\s*['"]miss['"]\s*\)\s*\?\s*false\s*:\s*\(\s*_ndState\s*\?\s*_ndGood\s*:\s*\(\s*hit\s*\|\|\s*\(\s*n\.f\s*>\s*0\s*&&\s*inGhostWin\s*\)\s*\)\s*\)/, '_showHit must honor a provider "miss" and fall back to the hit/ghost heuristic only with no verdict');
});
