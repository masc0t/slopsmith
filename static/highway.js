/**
 * Canvas-based Rocksmith highway renderer.
 * Receives note data via WebSocket, renders on requestAnimationFrame.
 */
function createHighway() {
    let canvas, ctx, ws;
    let currentTime = 0;
    let animFrame = null;
    let _connectOpts = {};
    let _resizeContainer = null;
    let _resizeHandler = null;
    let _onLyricsChange = null;

    // Song data (populated via WebSocket)
    let songInfo = {};
    let notes = [];
    let chords = [];
    let beats = [];
    let sections = [];
    let anchors = [];
    let chordTemplates = [];
    let lyrics = [];
    let toneChanges = [];
    let toneBase = "";
    let ready = false;
    let showLyrics = localStorage.getItem('showLyrics') !== 'false';
    let _drawHooks = [];  // plugin draw callbacks: fn(ctx, W, H)
    let _renderScale = parseFloat(localStorage.getItem('renderScale') || '1');  // 1 = full, 0.5 = half res
    let _inverted = localStorage.getItem('invertHighway') === 'true';
    function si(s) { return _inverted ? 5 - s : s; }  // string index mapper for inversion
    let _lefty = localStorage.getItem('lefty') === '1';

    // Rendering config
    const VISIBLE_SECONDS = 3.0;
    const Z_CAM = 2.2;
    const Z_MAX = 10.0;
    const BG = '#080810';

    const STRING_COLORS = [
        '#cc0000', '#cca800', '#0066cc',
        '#cc6600', '#00cc66', '#9900cc',
    ];
    const STRING_DIM = [
        '#520000', '#524200', '#002952',
        '#522900', '#005229', '#3d0052',
    ];
    const STRING_BRIGHT = [
        '#ff3c3c', '#ffe040', '#3c9cff',
        '#ff9c3c', '#3cff9c', '#cc3cff',
    ];

    // ── Projection ───────────────────────────────────────────────────────
    function project(tOffset) {
        if (tOffset > VISIBLE_SECONDS || tOffset < -0.05) return null;
        if (tOffset < 0) return { y: 0.82 + Math.abs(tOffset) * 0.3, scale: 1.0 };

        const z = tOffset * (Z_MAX / VISIBLE_SECONDS);
        const denom = z + Z_CAM;
        if (denom < 0.01) return null;
        const scale = Z_CAM / denom;
        const y = 0.82 + (0.08 - 0.82) * (1.0 - scale);
        return { y, scale };
    }

    // ── Anchor / Fret mapping ────────────────────────────────────────────
    // Zoom approach: fret 0 at the left edge, fret N at the right (entire canvas mirrored when lefty).
    // The "zoom level" determines how many frets are visible.
    // When playing low frets, zoom in (fewer frets visible, bigger notes).
    // When playing high frets, zoom out (more frets visible, smaller spacing).
    let displayMaxFret = 12;  // rightmost visible fret (smoothed)

    function getAnchorAt(t) {
        let a = anchors[0] || { fret: 1, width: 4 };
        for (const anc of anchors) {
            if (anc.time > t) break;
            a = anc;
        }
        return a;
    }

    function getMaxFretInWindow(t) {
        // Find the highest fret needed across all anchors visible on screen
        let maxFret = 0;
        for (const anc of anchors) {
            if (anc.time > t + VISIBLE_SECONDS) break;
            if (anc.time + 2 < t) continue;  // skip anchors well in the past
            const top = anc.fret + anc.width;
            if (top > maxFret) maxFret = top;
        }
        return maxFret;
    }

    function updateSmoothAnchor(anchor, dt) {
        const rate = Math.min(1.0 * dt, 1.0);
        // Look ahead: use the widest fret range across all visible anchors
        const lookAheadMax = getMaxFretInWindow(currentTime);
        const currentMax = anchor.fret + anchor.width;
        const needed = Math.max(currentMax, lookAheadMax);
        const targetMax = Math.max(needed + 3, 8);
        displayMaxFret += (targetMax - displayMaxFret) * rate;
    }

    function fretX(fret, scale, w) {
        const hw = w * 0.52 * scale;
        const margin = hw * 0.06;
        const usable = hw * 2 - 2 * margin;
        const t = fret / Math.max(1, displayMaxFret);
        return w / 2 - hw + margin + t * usable;
    }

    /** Call while lefty mirror transform is active; keeps glyphs readable. */
    function fillTextReadable(text, x, y) {
        if (!canvas) return;
        const W = canvas.width;
        if (!_lefty) {
            ctx.fillText(text, x, y);
            return;
        }
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillText(text, W - x, y);
        ctx.restore();
    }

    // ── Drawing ──────────────────────────────────────────────────────────
    function draw() {
        animFrame = requestAnimationFrame(draw);
        if (!canvas || !ready) return;
        try {
        const W = canvas.width;
        const H = canvas.height;
        ctx.fillStyle = BG;
        ctx.fillRect(0, 0, W, H);

        const anchor = getAnchorAt(currentTime);
        updateSmoothAnchor(anchor, 1 / 60);

        ctx.save();
        if (_lefty) {
            ctx.translate(W, 0);
            ctx.scale(-1, 1);
        }

        drawHighway(W, H);
        drawFretLines(W, H);
        drawBeats(W, H);
        drawStrings(W, H);
        drawSustains(W, H);
        drawNowLine(W, H);
        drawNotes(W, H);
        drawChords(W, H);
        drawFretNumbers(W, H);

        // Plugin draw hooks (same coordinate system as the highway)
        for (const hook of _drawHooks) {
            try { hook(ctx, W, H); } catch (e) { /* ignore */ }
        }

        ctx.restore();

        // Lyrics: drawn unmirrored so lines stay left-to-right readable (layout is center-symmetric)
        if (showLyrics) drawLyrics(W, H);

        } catch (e) {
            console.error('draw error:', e);
        }
    }

    function drawHighway(W, H) {
        const strips = 40;
        for (let i = 0; i < strips; i++) {
            const t0 = (i / strips) * VISIBLE_SECONDS;
            const t1 = ((i + 1) / strips) * VISIBLE_SECONDS;
            const p0 = project(t0), p1 = project(t1);
            if (!p0 || !p1) continue;

            const hw0 = W * 0.26 * p0.scale;
            const hw1 = W * 0.26 * p1.scale;
            const bright = 18 + 10 * p0.scale;

            ctx.fillStyle = `rgb(${bright|0},${bright|0},${(bright+14)|0})`;
            ctx.beginPath();
            ctx.moveTo(W/2 - hw0, p0.y * H);
            ctx.lineTo(W/2 + hw0, p0.y * H);
            ctx.lineTo(W/2 + hw1, p1.y * H);
            ctx.lineTo(W/2 - hw1, p1.y * H);
            ctx.fill();
        }
    }

    function drawFretLines(W, H) {
        const pad = 3;
        const lo = 0;
        const hi = Math.ceil(displayMaxFret);
        ctx.strokeStyle = '#2d2d45';
        ctx.lineWidth = 1;

        for (let fret = lo; fret <= hi; fret++) {
            if (fret < 0) continue;
            ctx.beginPath();
            for (let i = 0; i <= 40; i++) {
                const t = (i / 40) * VISIBLE_SECONDS;
                const p = project(t);
                if (!p) continue;
                const x = fretX(fret, p.scale, W);
                if (i === 0) ctx.moveTo(x, p.y * H);
                else ctx.lineTo(x, p.y * H);
            }
            ctx.stroke();
        }
    }

    function drawBeats(W, H) {
        for (const beat of beats) {
            const tOff = beat.time - currentTime;
            const p = project(tOff);
            if (!p || p.scale < 0.06) continue;
            const hw = W * 0.26 * p.scale;
            const isMeasure = beat.measure >= 0;
            ctx.strokeStyle = isMeasure ? '#343450' : '#202038';
            ctx.lineWidth = isMeasure ? 2 : 1;
            ctx.beginPath();
            ctx.moveTo(W/2 - hw, p.y * H);
            ctx.lineTo(W/2 + hw, p.y * H);
            ctx.stroke();
        }
    }

    function drawStrings(W, H) {
        const strTop = H * 0.83;
        const strBot = H * 0.95;
        const margin = W * 0.03;
        for (let i = 0; i < 6; i++) {
            const yi = _inverted ? 5 - i : i;
            const y = strTop + (yi / 5) * (strBot - strTop);
            ctx.strokeStyle = STRING_COLORS[i];
            ctx.lineWidth = 3;
            ctx.beginPath();
            ctx.moveTo(margin, y);
            ctx.lineTo(W - margin, y);
            ctx.stroke();
        }
    }

    function drawNowLine(W, H) {
        const y = H * 0.82;
        const hw = W * 0.26;
        // Glow
        for (let i = 1; i < 5; i++) {
            const a = Math.max(0, 70 - i * 15);
            ctx.strokeStyle = `rgba(${a},${a},${a+8},1)`;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(W/2 - hw, y - i);
            ctx.lineTo(W/2 + hw, y - i);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(W/2 - hw, y + i);
            ctx.lineTo(W/2 + hw, y + i);
            ctx.stroke();
        }
        ctx.strokeStyle = '#dce0f0';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(W/2 - hw, y);
        ctx.lineTo(W/2 + hw, y);
        ctx.stroke();
    }

    function drawNote(W, H, x, y, scale, string, fret, opts) {
        const isHarmonic = opts?.hm || opts?.hp || false;
        const isPinchHarmonic = opts?.hp || false;
        const isChord = opts?.chord || false;
        const bend = opts?.bn || 0;
        const slide = opts?.sl || -1;
        const hammerOn = opts?.ho || false;
        const pullOff = opts?.po || false;
        const tap = opts?.tp || false;
        const palmMute = opts?.pm || false;
        const tremolo = opts?.tr || false;
        const accent = opts?.ac || false;
        const sz = Math.max(12, 80 * scale * (H / 900));
        const half = sz / 2;
        const color = STRING_COLORS[string] || '#888';
        const dark = STRING_DIM[string] || '#222';

        if (sz < 6) {
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(x, y, 2, 0, Math.PI * 2);
            ctx.fill();
            return;
        }

        // Open string: wide bar spanning the highway (only for standalone notes)
        if (fret === 0 && !isChord) {
            const hw = W * 0.26 * scale;
            const barH = Math.max(6, sz * 0.45);
            // Shadow
            ctx.fillStyle = dark;
            roundRect(ctx, W/2 - hw - 1, y - barH/2 - 1, hw * 2 + 2, barH + 2, 3);
            ctx.fill();
            // Body
            ctx.fillStyle = color;
            roundRect(ctx, W/2 - hw, y - barH/2, hw * 2, barH, 2);
            ctx.fill();
            // "0" label
            const fontSize = Math.max(8, sz * 0.5) | 0;
            ctx.fillStyle = '#fff';
            ctx.font = `bold ${fontSize}px sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            fillTextReadable('0', W/2, y);
            return;
        }

        if (isHarmonic) {
            // Diamond shape for harmonics
            const dh = half * 1.15;
            // Glow
            ctx.fillStyle = dark;
            ctx.beginPath();
            ctx.moveTo(x, y - dh - 3); ctx.lineTo(x + half + 3, y);
            ctx.lineTo(x, y + dh + 3); ctx.lineTo(x - half - 3, y);
            ctx.closePath(); ctx.fill();
            // Body
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.moveTo(x, y - dh); ctx.lineTo(x + half, y);
            ctx.lineTo(x, y + dh); ctx.lineTo(x - half, y);
            ctx.closePath(); ctx.fill();
            // Bright outline
            ctx.strokeStyle = STRING_BRIGHT[string] || '#fff';
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(x, y - dh); ctx.lineTo(x + half, y);
            ctx.lineTo(x, y + dh); ctx.lineTo(x - half, y);
            ctx.closePath(); ctx.stroke();
            // PH label for pinch harmonics
            if (isPinchHarmonic && sz >= 14) {
                ctx.fillStyle = '#ff0';
                ctx.font = `bold ${Math.max(8, sz * 0.25) | 0}px sans-serif`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'top';
                fillTextReadable('PH', x, y + dh + 2);
            }
        } else {
            // Glow
            ctx.fillStyle = dark;
            roundRect(ctx, x - half - 4, y - half - 4, sz + 8, sz + 8, sz / 3);
            ctx.fill();
            // Body
            ctx.fillStyle = color;
            roundRect(ctx, x - half, y - half, sz, sz, sz / 5);
            ctx.fill();
        }

        // Fret number
        const fontSize = Math.max(10, sz * 0.5) | 0;
        ctx.fillStyle = '#fff';
        ctx.font = `bold ${fontSize}px sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        fillTextReadable(String(fret), x, y);

        // Bend notation
        if (bend && bend > 0 && sz >= 12) {
            const lw = Math.max(2, sz / 10);
            const arrowH = sz * 0.55 * Math.min(bend, 2);  // taller for bigger bends
            const ay = y - half - 4;
            const tipY = ay - arrowH;

            ctx.strokeStyle = '#fff';
            ctx.lineWidth = lw;

            // Curved arrow
            ctx.beginPath();
            ctx.moveTo(x, ay);
            ctx.quadraticCurveTo(x + sz * 0.2, ay - arrowH * 0.5, x, tipY);
            ctx.stroke();

            // Arrowhead
            ctx.beginPath();
            ctx.moveTo(x - sz * 0.12, tipY + sz * 0.12);
            ctx.lineTo(x, tipY);
            ctx.lineTo(x + sz * 0.12, tipY + sz * 0.12);
            ctx.stroke();

            // Bend label: "full", "1/2", "1 1/2", "2"
            let label;
            if (bend === 0.5) label = '½';
            else if (bend === 1) label = 'full';
            else if (bend === 1.5) label = '1½';
            else if (bend === 2) label = '2';
            else label = bend.toFixed(1);

            ctx.fillStyle = '#fff';
            ctx.font = `bold ${Math.max(9, sz * 0.28) | 0}px sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            fillTextReadable(label, x, tipY - 2);
        }

        if (sz < 14) return;  // Skip small technique labels

        // Slide indicator (diagonal arrow)
        if (slide >= 0) {
            const dir = slide > fret ? -1 : 1;  // arrow direction (up or down the neck); mirror handles lefty
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = Math.max(2, sz / 10);
            ctx.beginPath();
            ctx.moveTo(x - sz * 0.3, y + dir * sz * 0.3);
            ctx.lineTo(x + sz * 0.3, y - dir * sz * 0.3);
            ctx.stroke();
            // Arrowhead
            ctx.beginPath();
            ctx.moveTo(x + sz * 0.3, y - dir * sz * 0.3);
            ctx.lineTo(x + sz * 0.15, y - dir * sz * 0.15);
            ctx.stroke();
        }

        // H/P/T label above note
        if (hammerOn || pullOff || tap) {
            const label = tap ? 'T' : (hammerOn ? 'H' : 'P');
            const ly = y - half - (bend > 0 ? sz * 0.6 : 4);
            ctx.fillStyle = '#fff';
            ctx.font = `bold ${Math.max(9, sz * 0.3) | 0}px sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            fillTextReadable(label, x, ly);
        }

        // Palm mute (PM below note)
        if (palmMute) {
            ctx.fillStyle = '#aaa';
            ctx.font = `bold ${Math.max(8, sz * 0.25) | 0}px sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            fillTextReadable('PM', x, y + half + 2);
        }

        // Tremolo (wavy line above)
        if (tremolo) {
            const ty = y - half - (bend > 0 ? sz * 0.7 : 6);
            ctx.strokeStyle = '#ff0';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            for (let i = -3; i <= 3; i++) {
                const wx = x + i * sz * 0.08;
                const wy = ty + Math.sin(i * 2) * 3;
                if (i === -3) ctx.moveTo(wx, wy);
                else ctx.lineTo(wx, wy);
            }
            ctx.stroke();
        }

        // Accent (> marker)
        if (accent) {
            const ay2 = y - half - 4;
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(x - sz * 0.2, ay2 + 3);
            ctx.lineTo(x, ay2 - 2);
            ctx.lineTo(x + sz * 0.2, ay2 + 3);
            ctx.stroke();
        }
    }

    function drawSustains(W, H) {
        for (const n of notes) {
            if (n.sus <= 0.01) continue;
            const end = n.t + n.sus;
            if (end < currentTime || n.t > currentTime + VISIBLE_SECONDS) continue;

            const t0 = Math.max(n.t - currentTime, 0);
            const t1 = Math.min(end - currentTime, VISIBLE_SECONDS);
            if (t0 >= t1) continue;

            const p0 = project(t0), p1 = project(t1);
            if (!p0 || !p1) continue;

            const x0 = fretX(n.f, p0.scale, W);
            const x1 = fretX(n.f, p1.scale, W);
            const sw0 = Math.max(2, 6 * p0.scale);
            const sw1 = Math.max(2, 6 * p1.scale);

            ctx.fillStyle = STRING_DIM[n.s] || '#333';
            ctx.beginPath();
            ctx.moveTo(x0 - sw0, p0.y * H);
            ctx.lineTo(x0 + sw0, p0.y * H);
            ctx.lineTo(x1 + sw1, p1.y * H);
            ctx.lineTo(x1 - sw1, p1.y * H);
            ctx.fill();
        }
    }

    function drawNotes(W, H) {
        // Binary search for visible range
        const tMin = currentTime - 0.25;
        const tMax = currentTime + VISIBLE_SECONDS;
        let lo = bsearch(notes, tMin);
        let hi = bsearch(notes, tMax);

        // Include sustained notes
        while (lo > 0 && notes[lo-1].t + notes[lo-1].sus > currentTime) lo--;

        // Collect drawn positions for unison bend detection
        const drawnNotes = [];

        for (let i = hi - 1; i >= lo; i--) {
            const n = notes[i];
            let tOff = n.t - currentTime;

            // Hold sustained notes at now line
            let p;
            if (tOff < -0.05 && n.sus > 0 && n.t + n.sus > currentTime) {
                p = { y: 0.82, scale: 1.0 };
            } else {
                p = project(tOff);
            }
            if (!p) continue;

            const x = fretX(n.f, p.scale, W);
            drawNote(W, H, x, p.y * H, p.scale, n.s, n.f, n);
            drawnNotes.push({ t: n.t, s: n.s, f: n.f, bn: n.bn || 0, x, y: p.y * H, scale: p.scale });
        }

        // Draw unison bend connectors
        drawUnisonBends(W, H, drawnNotes);
    }

    function drawUnisonBends(W, H, drawnNotes) {
        // Group notes by time (within 0.01s tolerance)
        const groups = [];
        const used = new Set();
        for (let i = 0; i < drawnNotes.length; i++) {
            if (used.has(i)) continue;
            const group = [drawnNotes[i]];
            used.add(i);
            for (let j = i + 1; j < drawnNotes.length; j++) {
                if (used.has(j)) continue;
                if (Math.abs(drawnNotes[j].t - drawnNotes[i].t) < 0.01) {
                    group.push(drawnNotes[j]);
                    used.add(j);
                }
            }
            if (group.length >= 2) groups.push(group);
        }

        for (const group of groups) {
            // Find pairs: one with bend, one without (or both with different bends)
            const bent = group.filter(n => n.bn > 0);
            const unbent = group.filter(n => n.bn === 0);
            if (bent.length === 0 || unbent.length === 0) continue;

            // Draw connector between each bent-unbent pair
            for (const bn of bent) {
                // Find the closest unbent note by string
                let closest = unbent[0];
                for (const ub of unbent) {
                    if (Math.abs(ub.s - bn.s) < Math.abs(closest.s - bn.s)) closest = ub;
                }

                const sz = Math.max(12, 80 * bn.scale * (H / 900));
                if (sz < 14) continue;

                // Draw a curved dashed line connecting bent note to target note
                const x1 = bn.x, y1 = bn.y;
                const x2 = closest.x, y2 = closest.y;
                const midX = (x1 + x2) / 2 + sz * 0.5;
                const midY = (y1 + y2) / 2;

                ctx.save();
                ctx.strokeStyle = '#60d0ff';
                ctx.lineWidth = Math.max(2, sz / 12);
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(x1, y1);
                ctx.quadraticCurveTo(midX, midY, x2, y2);
                ctx.stroke();
                ctx.setLineDash([]);
                ctx.restore();

                // "U" label at midpoint
                const labelSz = Math.max(10, sz * 0.3) | 0;
                ctx.fillStyle = '#60d0ff';
                ctx.font = `bold ${labelSz}px sans-serif`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                const cpX = (x1 + 2 * midX + x2) / 4;
                const cpY = (y1 + 2 * midY + y2) / 4;
                fillTextReadable('U', cpX + sz * 0.3, cpY);
            }
        }
    }

    function drawChords(W, H) {
        const tMin = currentTime - 0.25;
        const tMax = currentTime + VISIBLE_SECONDS;
        let lo = bsearchChords(chords, tMin);
        let hi = bsearchChords(chords, tMax);

        for (let i = hi - 1; i >= lo; i--) {
            const ch = chords[i];
            const p = project(ch.t - currentTime);
            if (!p) continue;

            const sorted = [...ch.notes].sort((a, b) => _inverted ? b.s - a.s : a.s - b.s);
            const sz = Math.max(10, 28 * p.scale * (H / 900));
            const spread = sz * 0.85;
            const totalH = spread * (sorted.length - 1);

            // Bracket connector
            const minSpread = sz + 16;  // full note size + gap (accounts for glow)
            const actualSpread = Math.max(spread, minSpread);
            const actualTotalH = actualSpread * (sorted.length - 1);
            if (sorted.length >= 2) {
                const positions = sorted.map((cn, j) => ({
                    x: fretX(cn.f, p.scale, W),
                    y: p.y * H - actualTotalH / 2 + j * actualSpread,
                }));
                const barY = positions[0].y - sz * 0.7;

                ctx.fillStyle = '#50a0dc';
                ctx.lineWidth = Math.max(3, sz / 4);
                // Horizontal bar
                const xMin = Math.min(...positions.map(p => p.x));
                const xMax = Math.max(...positions.map(p => p.x));
                roundRect(ctx, xMin - 2, barY - 2, xMax - xMin + 4, 4, 2);
                ctx.fill();
                // Stems
                for (const pos of positions) {
                    ctx.fillRect(pos.x - 2, barY, 4, pos.y - sz/2 - barY);
                }
            }

            // Chord name label
            if (!ch.hd && p.scale > 0.15) {
                const tmpl = chordTemplates[ch.id];
                if (tmpl && tmpl.name) {
                    const labelY = (sorted.length >= 2)
                        ? (p.y * H - actualTotalH / 2 - sz * 0.7 - sz * 0.4)
                        : (p.y * H - sz * 0.8);
                    const labelX = (sorted.length >= 2)
                        ? (Math.min(...sorted.map(cn => fretX(cn.f, p.scale, W))) + Math.max(...sorted.map(cn => fretX(cn.f, p.scale, W)))) / 2
                        : fretX(sorted[0].f, p.scale, W);
                    ctx.fillStyle = '#fff';
                    ctx.font = `bold ${Math.max(14, sz * 0.45) | 0}px sans-serif`;
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'bottom';
                    fillTextReadable(tmpl.name, labelX, labelY);
                }
            }

            // Notes — ensure same-fret notes don't overlap vertically
            const chordPositions = [];
            sorted.forEach((cn, j) => {
                const x = fretX(cn.f, p.scale, W);
                const ny = p.y * H - actualTotalH / 2 + j * actualSpread;
                drawNote(W, H, x, ny, p.scale, cn.s, cn.f, { ...cn, chord: true });
                chordPositions.push({ s: cn.s, f: cn.f, bn: cn.bn || 0, x, y: ny, scale: p.scale });
            });

            // Unison bend within chord
            const bent = chordPositions.filter(n => n.bn > 0);
            const unbent = chordPositions.filter(n => n.bn === 0);
            if (bent.length > 0 && unbent.length > 0 && sz >= 14) {
                for (const bn of bent) {
                    let closest = unbent[0];
                    for (const ub of unbent) {
                        if (Math.abs(ub.s - bn.s) < Math.abs(closest.s - bn.s)) closest = ub;
                    }
                    const x1 = bn.x, y1 = bn.y;
                    const x2 = closest.x, y2 = closest.y;
                    const midX = (x1 + x2) / 2 + sz * 0.5;
                    const midY = (y1 + y2) / 2;

                    ctx.save();
                    ctx.strokeStyle = '#60d0ff';
                    ctx.lineWidth = Math.max(2, sz / 12);
                    ctx.setLineDash([4, 4]);
                    ctx.beginPath();
                    ctx.moveTo(x1, y1);
                    ctx.quadraticCurveTo(midX, midY, x2, y2);
                    ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.restore();

                    const labelSz = Math.max(10, sz * 0.3) | 0;
                    ctx.fillStyle = '#60d0ff';
                    ctx.font = `bold ${labelSz}px sans-serif`;
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    const cpX = (x1 + 2 * midX + x2) / 4;
                    const cpY = (y1 + 2 * midY + y2) / 4;
                    fillTextReadable('U', cpX + sz * 0.3, cpY);
                }
            }
        }
    }

    function drawFretNumbers(W, H) {
        const y = H * 0.97;
        const pad = 3;
        const lo = 0;
        const hi = Math.ceil(displayMaxFret);
        const anchor = getAnchorAt(currentTime);

        ctx.font = 'bold 20px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        for (let fret = lo; fret <= hi; fret++) {
            if (fret < 0) continue;
            const x = fretX(fret, 1.0, W);
            const inAnchor = fret >= anchor.fret && fret <= anchor.fret + anchor.width;
            ctx.fillStyle = inAnchor ? '#e8c040' : '#8a6830';
            fillTextReadable(String(fret), x, y);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    function drawLyrics(W, H) {
        if (!lyrics.length) return;

        const fontSize = Math.max(18, H * 0.028) | 0;
        const lineY = H * 0.04;

        // Rocksmith vocal markers: a trailing "-" means the syllable joins the
        // next one into a single word (no space); a trailing "+" marks the end
        // of an authored line. Build a flat list of authored lines so we can
        // cap rendering to a 2-line rolling window (current + upcoming).
        if (!lyrics._lines) {
            const lines = [];
            let line = null, word = null;

            const flushWord = () => {
                if (word && word.length) line.words.push(word);
                word = null;
            };
            const flushLine = () => {
                flushWord();
                if (line && line.words.length) lines.push(line);
                line = null;
            };

            for (let i = 0; i < lyrics.length; i++) {
                const l = lyrics[i];
                const raw = l.w || '';
                const endsLine = raw.endsWith('+');
                const continuesWord = raw.endsWith('-');

                // Safety fallback: if a song has no "+" markers at all, force a
                // line break on any gap > 4s so we never build a single giant line.
                if (line && i > 0) {
                    const prev = lyrics[i - 1];
                    if (l.t - (prev.t + prev.d) > 4.0) flushLine();
                }

                if (!line) line = { words: [], start: l.t, end: l.t + l.d };
                if (!word) word = [];

                word.push(l);
                line.end = Math.max(line.end, l.t + l.d);

                if (!continuesWord) flushWord();
                if (endsLine) flushLine();
            }
            flushLine();

            lyrics._lines = lines;
        }

        const allLines = lyrics._lines;
        if (!allLines.length) return;

        // Current line = most recently started line. Before the first line has
        // started, preview the first line if it's within 2s of starting.
        let currentIdx = -1;
        for (let i = 0; i < allLines.length; i++) {
            if (allLines[i].start <= currentTime) currentIdx = i;
            else break;
        }
        if (currentIdx === -1) {
            if (allLines[0].start - currentTime > 2.0) return;
            currentIdx = 0;
        }

        const currentLine = allLines[currentIdx];
        const nextLine = allLines[currentIdx + 1] || null;
        const gapToNext = nextLine ? (nextLine.start - currentLine.end) : Infinity;

        // Hide once the current line is clearly over and nothing relevant follows.
        if (currentTime > currentLine.end + 0.5 && gapToNext > 3.0) return;

        const linesToShow = [currentLine];
        if (nextLine && gapToNext <= 3.0) linesToShow.push(nextLine);

        const sylText = (s) => {
            const t = s.w || '';
            return (t.endsWith('+') || t.endsWith('-')) ? t.slice(0, -1) : t;
        };

        ctx.font = `bold ${fontSize}px sans-serif`;
        const spaceWidth = ctx.measureText(' ').width;
        const maxWidth = W * 0.8;

        // Respect authored line breaks; wrap only if a line overflows maxWidth.
        const rows = [];
        for (const authoredLine of linesToShow) {
            let row = [], rowWidth = 0;
            for (const wordSyls of authoredLine.words) {
                const parts = [];
                let wordWidth = 0;
                for (const s of wordSyls) {
                    const text = sylText(s);
                    const w = ctx.measureText(text).width;
                    parts.push({ syl: s, text, width: w });
                    wordWidth += w;
                }
                const advance = wordWidth + spaceWidth;
                if (row.length > 0 && rowWidth + advance > maxWidth) {
                    rows.push(row);
                    row = []; rowWidth = 0;
                }
                row.push({ parts, advance });
                rowWidth += advance;
            }
            if (row.length) rows.push(row);
        }

        const rowHeight = fontSize + 6;
        const totalHeight = rows.length * rowHeight + 10;
        let bgWidth = 0;
        for (const row of rows) {
            const rw = row.reduce((s, w) => s + w.advance, 0) - spaceWidth;
            if (rw > bgWidth) bgWidth = rw;
        }
        bgWidth = Math.min(bgWidth + 30, W * 0.85);

        ctx.fillStyle = 'rgba(0,0,0,0.7)';
        roundRect(ctx, W/2 - bgWidth/2, lineY - 4, bgWidth, totalHeight, 8);
        ctx.fill();

        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';

        for (let r = 0; r < rows.length; r++) {
            const row = rows[r];
            const rowWidth = row.reduce((s, w) => s + w.advance, 0) - spaceWidth;
            let xPos = W/2 - rowWidth/2;
            const yPos = lineY + r * rowHeight + 2;

            for (const w of row) {
                for (const part of w.parts) {
                    const l = part.syl;
                    const isActive = currentTime >= l.t && currentTime < l.t + l.d;
                    const isPast = currentTime >= l.t + l.d;

                    if (isActive) {
                        ctx.fillStyle = '#4ae0ff';
                        ctx.font = `bold ${fontSize}px sans-serif`;
                    } else if (isPast) {
                        ctx.fillStyle = '#8899aa';
                        ctx.font = `normal ${fontSize}px sans-serif`;
                    } else {
                        ctx.fillStyle = '#556677';
                        ctx.font = `normal ${fontSize}px sans-serif`;
                    }

                    ctx.fillText(part.text, xPos, yPos);
                    xPos += part.width;
                }
                xPos += spaceWidth;
            }
        }
    }

    function roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
    }

    function bsearch(arr, time) {
        let lo = 0, hi = arr.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (arr[mid].t < time) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }
    function bsearchChords(arr, time) {
        let lo = 0, hi = arr.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (arr[mid].t < time) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    // ── Public API ───────────────────────────────────────────────────────
    const api = {
        init(canvasEl, container) {
            canvas = canvasEl;
            _resizeContainer = container || null;
            ctx = canvas.getContext('2d');
            this.resize();
            if (_resizeHandler) window.removeEventListener('resize', _resizeHandler);
            _resizeHandler = () => this.resize();
            window.addEventListener('resize', _resizeHandler);
            ready = false;
            notes = []; chords = []; beats = []; sections = []; anchors = []; lyrics = []; toneChanges = []; toneBase = "";
        },

        resize() {
            if (!canvas) return;
            let w, h;
            if (_resizeContainer) {
                const rect = _resizeContainer.getBoundingClientRect();
                w = rect.width;
                h = rect.height;
            } else {
                const controls = document.getElementById('player-controls');
                const controlsH = controls ? controls.offsetHeight : 50;
                w = document.documentElement.clientWidth;
                h = document.documentElement.clientHeight - controlsH;
            }
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
            canvas.width = Math.round(w * _renderScale);
            canvas.height = Math.round(h * _renderScale);
        },

        setRenderScale(scale) {
            _renderScale = Math.max(0.25, Math.min(1, scale));
            localStorage.setItem('renderScale', _renderScale);
            this.resize();
        },

        getRenderScale() { return _renderScale; },

        getInverted() { return _inverted; },
        setInverted(v) { _inverted = v; localStorage.setItem('invertHighway', v); },
        setLefty(on) {
            _lefty = !!on;
            localStorage.setItem('lefty', _lefty ? '1' : '0');
        },

        getLefty() { return _lefty; },

        connect(wsUrl, opts = {}) {
            _connectOpts = opts;
            ws = new WebSocket(wsUrl);
            ws.onclose = () => { console.log('WS closed'); };
            ws.onerror = (e) => { console.error('WS error', e); };
            ws.onmessage = (ev) => {
                const msg = JSON.parse(ev.data);
                if (msg.error) {
                    console.error('Server error:', msg.error);
                    if (opts.onError) opts.onError(msg.error);
                    else alert('Error: ' + msg.error);
                    return;
                }
                switch (msg.type) {
                    case 'loading':
                        console.log('Loading:', msg.stage);
                        break;
                    case 'song_info':
                        songInfo = msg;
                        if (opts.onSongInfo) {
                            opts.onSongInfo(msg);
                        } else {
                            document.getElementById('hud-artist').textContent = msg.artist;
                            document.getElementById('hud-title').textContent = msg.title;
                            document.getElementById('hud-arrangement').textContent = msg.arrangement;

                            // Clear any lingering audio-error banner from a prior song.
                            const existingAudioErr = document.getElementById('audio-error-banner');
                            if (existingAudioErr) existingAudioErr.remove();

                            // Server reported a concrete audio-pipeline failure and has
                            // no URL to give us — surface it instead of leaving the
                            // user with a cryptic "Empty src attribute" from audio.play().
                            if (!msg.audio_url && msg.audio_error) {
                                const banner = document.createElement('div');
                                banner.id = 'audio-error-banner';
                                banner.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-[300] bg-red-900/95 border border-red-700 text-red-100 rounded-lg px-4 py-3 max-w-2xl shadow-xl';
                                banner.innerHTML = `
                                    <div class="flex items-start gap-3">
                                        <span class="text-xl leading-none">⚠</span>
                                        <div class="flex-1">
                                            <div class="font-semibold text-sm">Audio unavailable</div>
                                            <div class="text-xs text-red-200 mt-1"></div>
                                        </div>
                                        <button class="text-red-300 hover:text-white text-lg leading-none" aria-label="Dismiss">✕</button>
                                    </div>`;
                                banner.querySelector('.text-xs').textContent = msg.audio_error;
                                banner.querySelector('button').addEventListener('click', () => banner.remove());
                                document.body.appendChild(banner);
                            }

                            if (msg.audio_url) {
                                const audio = document.getElementById('audio');
                                if (!audio.src || !audio.src.includes(msg.audio_url.split('/').pop())) {
                                    audio.src = msg.audio_url;
                                    audio.load();

                                    // Show buffering overlay
                                    let overlay = document.getElementById('audio-buffer-overlay');
                                    if (!overlay) {
                                        overlay = document.createElement('div');
                                        overlay.id = 'audio-buffer-overlay';
                                        overlay.className = 'fixed inset-0 z-[200] flex items-center justify-center bg-black/60 backdrop-blur-sm';
                                        overlay.innerHTML = `
                                            <div class="bg-dark-700 border border-gray-700 rounded-2xl p-6 w-72 text-center shadow-2xl">
                                                <div class="text-sm text-gray-300 mb-3">Loading audio...</div>
                                                <div style="height:6px;background:#1a1a2e;border-radius:999px;overflow:hidden">
                                                    <div id="audio-buffer-bar" style="height:100%;background:linear-gradient(90deg,#4080e0,#60a0ff);border-radius:999px;width:0%;transition:width 0.3s"></div>
                                                </div>
                                                <div class="text-xs text-gray-500 mt-2" id="audio-buffer-pct">0%</div>
                                            </div>`;
                                        document.body.appendChild(overlay);
                                    }

                                    const bar = document.getElementById('audio-buffer-bar');
                                    const pct = document.getElementById('audio-buffer-pct');

                                    const MIN_BUFFER_SECS = 30;

                                    function onProgress() {
                                        if (audio.buffered.length > 0 && audio.duration > 0) {
                                            const loaded = audio.buffered.end(audio.buffered.length - 1);
                                            const p = Math.round((loaded / audio.duration) * 100);
                                            if (bar) bar.style.width = p + '%';
                                            if (pct) pct.textContent = p + '%';
                                            // Dismiss when enough is buffered
                                            if (loaded >= MIN_BUFFER_SECS || loaded >= audio.duration) {
                                                cleanup();
                                            }
                                        }
                                    }

                                    function cleanup() {
                                        audio.removeEventListener('progress', onProgress);
                                        audio.removeEventListener('canplaythrough', cleanup);
                                        const ol = document.getElementById('audio-buffer-overlay');
                                        if (ol) ol.remove();
                                    }

                                    audio.addEventListener('progress', onProgress);
                                    // Fallback: also dismiss on canplaythrough
                                    audio.addEventListener('canplaythrough', cleanup, { once: true });
                                }
                            }
                            // Populate arrangement dropdown
                            if (msg.arrangements) {
                                const sel = document.getElementById('arr-select');
                                sel.innerHTML = msg.arrangements.map(a =>
                                    `<option value="${a.index}" ${a.index === msg.arrangement_index ? 'selected' : ''}>${a.name} (${a.notes})</option>`
                                ).join('');
                            }
                        }
                        // Plugin context API — broadcast current song state
                        if (window.slopsmith) {
                            const wsPath = ws.url.split('/ws/highway/')[1] || '';
                            const filename = decodeURIComponent(wsPath.split('?')[0]);
                            window.slopsmith.currentSong = {
                                filename,
                                title: msg.title,
                                artist: msg.artist,
                                duration: msg.duration,
                                arrangement: msg.arrangement,
                                arrangementIndex: msg.arrangement_index,
                                arrangements: msg.arrangements || [],
                                tuning: msg.tuning,
                                capo: msg.capo,
                                format: msg.format,
                            };
                            window.slopsmith.emit('song:loaded', window.slopsmith.currentSong);
                        }
                        break;
                    case 'beats': beats = msg.data; break;
                    case 'sections': sections = msg.data; break;
                    case 'anchors':
                        anchors = msg.data;
                        if (anchors.length) {
                            displayMaxFret = Math.max(anchors[0].fret + anchors[0].width + 3, 8);
                        }
                        break;
                    case 'chord_templates': chordTemplates = msg.data; break;
                    case 'lyrics': lyrics = msg.data; break;
                    case 'tone_changes': toneChanges = msg.data; toneBase = msg.base || ""; break;
                    case 'notes': notes = notes.concat(msg.data); break;
                    case 'chords': chords = chords.concat(msg.data); break;
                    case 'ready':
                        ready = true;
                        console.log(`Highway ready: ${notes.length} notes, ${chords.length} chords`);
                        if (!animFrame) draw();
                        if (api._onReady) api._onReady();
                        break;
                }
            };
        },

        setTime(t) { currentTime = t; },

        getBPM(t) {
            // Calculate BPM from beat intervals near time t
            if (beats.length < 2) return 120;
            let closest = 0;
            for (let i = 1; i < beats.length; i++) {
                if (Math.abs(beats[i].time - t) < Math.abs(beats[closest].time - t)) closest = i;
            }
            // Average interval from nearby beats
            const start = Math.max(0, closest - 2);
            const end = Math.min(beats.length - 1, closest + 2);
            let sum = 0, count = 0;
            for (let i = start; i < end; i++) {
                sum += beats[i + 1].time - beats[i].time;
                count++;
            }
            return count > 0 ? 60 / (sum / count) : 120;
        },

        getBeats() { return beats; },
        getTime() { return currentTime; },
        getNotes() { return notes; },
        getChords() { return chords; },
        getToneChanges() { return toneChanges; },
        getToneBase() { return toneBase; },
        getSections() { return sections; },
        getSongInfo() { return songInfo; },
        addDrawHook(fn) { _drawHooks.push(fn); },
        removeDrawHook(fn) { _drawHooks = _drawHooks.filter(h => h !== fn); },
        project(tOffset) { return project(tOffset); },
        fretX(fret, scale, w) { return fretX(fret, scale, w); },

        /** Use when drawing text inside the lefty mirror; noop when not lefty. */
        fillTextUnmirrored(text, x, y) { fillTextReadable(text, x, y); },

        toggleLyrics() {
            showLyrics = !showLyrics;
            localStorage.setItem('showLyrics', String(showLyrics));
            if (_onLyricsChange) _onLyricsChange(showLyrics);
        },

        getLyricsVisible() { return showLyrics; },
        setLyricsVisible(v) {
            showLyrics = !!v;
            if (_onLyricsChange) _onLyricsChange(showLyrics);
        },
        setOnLyricsChange(fn) { _onLyricsChange = fn; },

        reconnect(filename, arrangement) {
            // Close old WS but keep audio + animation running
            if (ws) { ws.close(); ws = null; }
            ready = false;
            notes = []; chords = []; beats = []; sections = []; anchors = []; lyrics = []; toneChanges = []; toneBase = "";
            const arrParam = arrangement !== undefined ? `?arrangement=${arrangement}` : '';
            // filename might already be encoded from data-play attribute
            const decoded = decodeURIComponent(filename);
            const wsUrl = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/highway/${decoded}${arrParam}`;
            console.log('reconnect:', wsUrl);
            this.connect(wsUrl, _connectOpts);
        },

        stop() {
            if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
            if (ws) { ws.close(); ws = null; }
            if (_resizeHandler) {
                window.removeEventListener('resize', _resizeHandler);
                _resizeHandler = null;
            }
            ready = false;
        },
    };
    return api;
}
const highway = createHighway();
window.highway = highway; // expose for plugins
highway.setOnLyricsChange(function(visible) {
    const btn = document.getElementById('btn-lyrics');
    if (btn) {
        btn.textContent = visible ? 'Lyrics \u2713' : 'Lyrics \u2717';
        btn.className = visible
            ? 'px-3 py-1.5 bg-purple-900/40 hover:bg-purple-900/60 rounded-lg text-xs text-purple-300 transition'
            : 'px-3 py-1.5 bg-dark-600 hover:bg-dark-500 rounded-lg text-xs text-gray-500 transition';
    }
});
