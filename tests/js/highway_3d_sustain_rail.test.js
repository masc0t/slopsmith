// Pins the chord sustain-length rail indicator in plugins/highway_3d/screen.js
// (PR #303). The rails are left/right edge plane meshes showing how long a
// chord is held. A refactor that drops the !isRepeat gate, mixes up the
// arpeggio/teal color choice, or changes the rail renderOrder would silently
// regress the indicator (rails on every repeat frame, wrong tint, or rails
// occluding note gems).
//
// Source-level only — same strategy as the other tests/js/ files.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');

test('sustain rails are gated on multi-note, non-repeat chords within AHEAD', () => {
    // Repeat frames in a chord sequence must not each draw their own rails,
    // and single notes have no chord frame to anchor a rail to.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /if\s*\(\s*chShape\.size\s*>\s*1\s*&&\s*chordOpenBoxW\s*!=\s*null\s*&&\s*!isRepeat\s*&&\s*chDt\s*<\s*AHEAD\s*\)/,
        'sustain-rail block must stay gated on chShape.size > 1, chordOpenBoxW, !isRepeat and chDt < AHEAD',
    );
});

test('sustain rails pick arpeggio color for arpeggio frames, teal otherwise', () => {
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /chordHighwayLavenderArpVisual\s*\?\s*ARPEGGIO_RIM_BLUE_HEX\s*:\s*CHORD_BOX_TEAL_HEX/,
        'rail color must select ARPEGGIO_RIM_BLUE_HEX for arpeggio frames and CHORD_BOX_TEAL_HEX for chords',
    );
});

test('sustain-rail pool meshes keep renderOrder 16 so note gems (20/21) stay on top', () => {
    // renderOrder 16 sits above the chord frame (12/13) but below note
    // outline/core (20/21). Bumping it past the notes would let the rails
    // occlude flying gems.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /pSusRail\s*=\s*pool\([^)]*,\s*\(\)\s*=>\s*\{[\s\S]*?m\.renderOrder\s*=\s*16\s*;[\s\S]*?\}\s*\)/,
        'pSusRail pool must seed meshes with renderOrder = 16',
    );
});
