"""
lib/gp2rs_gpx.py — Guitar Pro 6 (.gpx) support shim for gp2rs.

Drop this file into slopsmith/lib/ alongside gp2rs.py.
No third-party dependencies — pure Python stdlib only.

Public API mirrors the two functions that the editor plugin calls:
    list_tracks(gp_path)          -> list[dict]
    convert_file(gp_path, ...)    -> list[str]

Both are called transparently by gp2rs.py when the file extension is .gpx.
Do not call this module directly; use gp2rs.list_tracks / gp2rs.convert_file.
"""

import logging
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from safepath import safe_join

_log = logging.getLogger("slopsmith.lib.gp2rs_gpx")


def _safe_filename_stem(name: str) -> str:
    """Filesystem-safe stem from an untrusted (GPX-supplied) track name.

    Track names come from an arbitrary user-uploaded file, so they may contain
    path separators (``/`` or ``\\``), ``..``, colons, etc. Collapse anything
    outside ``[A-Za-z0-9._-]`` to ``_`` and strip leading/trailing dots/dashes
    so the result can't traverse out of the output directory on any platform.
    """
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', name or '').strip('._-')
    return s or 'track'

# Hard cap on the BCFZ-declared decompressed size. The size comes from a
# 32-bit field in an attacker-controllable upload; without a cap a crafted
# file could declare a multi-GB target and exhaust memory. Real GPX payloads
# are small (the November Rain example expands to ~1.4 MB); 64 MB is generous.
_GPX_MAX_DECOMPRESSED = 64 * 1024 * 1024

# ---------------------------------------------------------------------------
# GPX container parsing (BCFZ/BCFS)
# Identical to gpx_parser.py — duplicated here so lib/ is self-contained.
# ---------------------------------------------------------------------------

def _decompress_bcfz(raw: bytes) -> bytes:
    if raw[:4] != b'BCFZ':
        raise ValueError(f"Expected BCFZ magic, got {raw[:4]!r}")
    src = raw[4:]
    n = len(src)
    pos = 0; current_byte = 0; bit_pos = 8

    def _read_bit():
        nonlocal pos, current_byte, bit_pos
        if bit_pos >= 8:
            if pos >= n: raise EOFError()
            current_byte = src[pos]; pos += 1; bit_pos = 0
        val = (current_byte >> (7 - bit_pos)) & 1; bit_pos += 1; return val

    def _rb(count):
        v = 0
        for i in range(count - 1, -1, -1): v |= _read_bit() << i
        return v

    def _rr(count):
        v = 0
        for i in range(count): v |= _read_bit() << i
        return v

    lb = bytes(_rb(8) & 0xFF for _ in range(4))
    expected = struct.unpack_from('<I', lb)[0]
    if expected > _GPX_MAX_DECOMPRESSED:
        raise ValueError(
            f"GPX declares implausible decompressed size {expected} bytes "
            f"(> {_GPX_MAX_DECOMPRESSED} cap); refusing to decompress"
        )
    out = bytearray()
    try:
        while len(out) < expected:
            if _rb(1):
                ws = _rb(4); off = _rr(ws); sz = _rr(ws)
                sp = len(out) - off
                for i in range(min(off, sz)): out.append(out[sp + i])
            else:
                sz = _rr(2)
                for _ in range(sz): out.append(_rb(8) & 0xFF)
    except EOFError:
        pass
    return bytes(out)


def _parse_bcfs(bcfs: bytes) -> dict:
    if bcfs[:4] != b'BCFS':
        raise ValueError(f"Expected BCFS magic, got {bcfs[:4]!r}")
    HDR = 4; data = bcfs; SECTOR = 0x1000
    # A file can't reference more sectors than the container physically holds;
    # cap the sector-pointer walk so a malformed/crafted chain that never
    # yields a 0 terminator can't loop unbounded and exhaust memory.
    max_sectors = (len(data) // SECTOR) + 1

    def _gi(off):
        # Bounds-check every 4-byte read so a truncated/crafted file raises a
        # clean ValueError instead of leaking struct.error / IndexError.
        if off < 0 or HDR + off + 4 > len(data):
            raise ValueError("GPX BCFS read past end of container (malformed file)")
        return struct.unpack_from('<I', data, HDR + off)[0]
    def _gs(off, ml):
        base = HDR + off; end = base
        limit = min(base + ml, len(data))
        while end < limit and data[end] != 0: end += 1
        return data[base:end].decode('utf-8', errors='replace')

    files: dict = {}
    offset = SECTOR
    while HDR + offset + 4 <= len(data):
        if _gi(offset) == 2:
            fn = _gs(offset + 0x04, 127); fs = _gi(offset + 0x8C)
            po = offset + 0x94; sc = 0; fb = bytearray()
            while sc <= max_sectors:
                s = _gi(po + 4 * sc); sc += 1
                if s == 0: break
                so = s * SECTOR
                if HDR + so + SECTOR > len(data):
                    raise ValueError("GPX BCFS sector pointer out of range (malformed file)")
                fb.extend(data[HDR + so: HDR + so + SECTOR])
            else:
                raise ValueError("GPX BCFS sector chain too long (malformed file)")
            files[fn] = bytes(fb[:fs])
        offset += SECTOR
    return files


def _load_gpif(gp_path: str) -> ET.Element:
    """Load and parse score.gpif from a .gpx file."""
    with open(gp_path, 'rb') as fh:
        raw = fh.read()
    if raw[:4] == b'BCFZ':
        bcfs = _decompress_bcfz(raw)
    elif raw[:4] == b'BCFS':
        bcfs = raw
    else:
        raise ValueError(f"Not a GPX file (magic: {raw[:4]!r})")
    fs = _parse_bcfs(bcfs)
    if 'score.gpif' not in fs:
        raise ValueError("score.gpif not found in GPX container")
    return ET.fromstring(fs['score.gpif'])


# ---------------------------------------------------------------------------
# GPIF helpers
# ---------------------------------------------------------------------------

# GPX NoteValue string -> quarter-note multiplier
_NOTE_VALUE_QN = {
    'Whole': 4.0, 'Half': 2.0, 'Quarter': 1.0, 'Eighth': 0.5,
    '16th': 0.25, '32nd': 0.125, '64th': 0.0625, '128th': 0.03125,
}

# Diatonic step index (0=C) -> semitone offset
_STEP_TO_SEMI = [0, 2, 4, 5, 7, 9, 11]  # C D E F G A B


def _gpif_tempo(root: ET.Element) -> float:
    """Return the first Tempo automation value from MasterTrack.

    GPX stores tempo as e.g. "72 2" where the second token is an internal
    interpolation flag. Only the first token is the BPM value.
    """
    mt = root.find('MasterTrack')
    if mt is not None:
        for auto in mt.findall('.//Automations/*'):
            if auto.findtext('Type') == 'Tempo':
                raw = (auto.findtext('Value') or '').strip()
                try:
                    return float(raw.split()[0])
                except (ValueError, TypeError, IndexError):
                    pass
    return 120.0


def _build_tempo_map(root: ET.Element) -> list[tuple[int, float]]:
    """
    Build a bar-indexed tempo map from all Tempo automations on MasterTrack.

    Returns a list of (bar_index, bpm) sorted by bar_index. The map is used
    by convert_file() to step through bars at the correct tempo, which is
    critical for songs with multiple tempo changes (e.g. Bohemian Rhapsody
    goes from 72 -> 144 -> gradual slowdown to 32 BPM).

    GPX tempo Value format: "72 2" — first token = BPM, second = interpolation flag.
    """
    events: list[tuple[int, float]] = []
    mt = root.find('MasterTrack')
    if mt is not None:
        for auto in mt.findall('.//Automations/*'):
            if auto.findtext('Type') == 'Tempo':
                try:
                    bar = int(auto.findtext('Bar') or 0)
                    raw = (auto.findtext('Value') or '').strip()
                    bpm = float(raw.split()[0])
                    events.append((bar, bpm))
                except (ValueError, TypeError, IndexError):
                    pass
    events.sort(key=lambda e: e[0])
    if not events:
        events = [(0, 120.0)]
    return events


def _gpif_tracks(root: ET.Element) -> list[dict]:
    """Return a list of raw track dicts from the GPIF Tracks element."""
    # Lookups for per-track note counting. MasterBar/Bars lists one bar id per
    # track in raw Tracks order, so the enumerate index below (which counts
    # skipped pseudo-tracks) is the correct bar-lookup index — same mapping
    # convert_file uses via filtered_to_raw.
    _masterbars = list(root.find('MasterBars') or [])
    _bars_by_id = {b.get('id'): b for b in (root.find('Bars') or [])}
    _voices_by_id = {v.get('id'): v for v in (root.find('Voices') or [])}
    _beats_by_id = {b.get('id'): b for b in (root.find('Beats') or [])}

    def _note_count_for_raw(raw_idx: int) -> int:
        # Total note count for the track (sum of notes across all its beats).
        # This is the single source of truth: list_tracks surfaces it as the
        # 'notes' field, and _auto_select_gpx uses (count == 0) to skip empty
        # tracks — so the graph is walked once here, not again in list_tracks.
        n = 0
        for mb in _masterbars:
            bar_ids = mb.findtext('Bars', '').split()
            if raw_idx >= len(bar_ids):
                continue
            bar = _bars_by_id.get(bar_ids[raw_idx])
            if bar is None:
                continue
            for vid in bar.findtext('Voices', '').split():
                if vid == '-1':
                    continue
                voice = _voices_by_id.get(vid)
                if voice is None:
                    continue
                for bid in voice.findtext('Beats', '').split():
                    beat = _beats_by_id.get(bid)
                    if beat is None:
                        continue
                    notes_text = beat.findtext('Notes', '').strip()
                    if notes_text:
                        n += len(notes_text.split())
        return n

    result = []
    for raw_idx, t in enumerate(root.find('Tracks') or []):
        name = (t.findtext('Name') or '').strip()
        if name.startswith('@$') and name.endswith('$@'):
            continue  # GP internal pseudo-tracks (raw_idx still advances)

        gm = t.find('GeneralMidi')
        midi_program = 0
        midi_channel = 0
        is_drums = False
        if gm is not None:
            try: midi_program = int(gm.findtext('Program') or 0)
            except (ValueError, TypeError): pass
            try:
                ch = int(gm.findtext('PrimaryChannel') or 0)
                midi_channel = ch
                if ch == 9: is_drums = True
            except (ValueError, TypeError): pass
            if gm.get('table') == 'Percussion':
                is_drums = True

        # String tuning
        string_pitches: list[int] = []
        for prop in t.findall('.//Property'):
            if prop.get('name') == 'Tuning':
                pe = prop.find('Pitches')
                if pe is not None and pe.text:
                    try:
                        string_pitches = [int(p) for p in pe.text.split()]
                    except ValueError:
                        pass

        result.append({
            '_el': t,
            'id': t.get('id', ''),
            'name': name,
            'string_pitches': string_pitches,
            'is_drums': is_drums,
            'midi_program': midi_program,
            'midi_channel': midi_channel,
            'note_count': _note_count_for_raw(raw_idx),
        })
    return result


def _beat_dur_secs(beat_el: ET.Element, rhythms_dict: dict, tempo_bpm: float) -> float:
    """Return the duration of a beat in seconds."""
    rref = beat_el.find('Rhythm')
    dur_qn = 0.25
    if rref is not None:
        rhythm = rhythms_dict.get(rref.get('ref', ''))
        if rhythm is not None:
            nv = rhythm.findtext('NoteValue', 'Quarter')
            dur_qn = _NOTE_VALUE_QN.get(nv, 0.25)
            if rhythm.find('AugmentationDot') is not None:
                dur_qn *= 1.5
            # Tuplets
            tuplet = rhythm.find('PrimaryTuplet')
            if tuplet is not None:
                try:
                    num = int(tuplet.get('num', 1))
                    den = int(tuplet.get('den', 1))
                    if num and den:
                        dur_qn *= den / num
                except (TypeError, ValueError):
                    pass
    return dur_qn * (60.0 / tempo_bpm)


# ---------------------------------------------------------------------------
# Drum encoding tables — ported from alphaTab PercussionMapper (MIT licensed)
# ---------------------------------------------------------------------------

# GP6 Element+Variation -> articulation ID
# _GP6_EV[element][variation] = articulation_id
# Source: alphaTab PercussionMapper._gp6ElementAndVariationToArticulation
_GP6_EV: list[list[int]] = [
    [35, 35, 35],    # [0]  Kick (hit, -, -)
    [38, 91, 37],    # [1]  Snare (hit, rim shot, side stick)
    [99, 100, 99],   # [2]  Cowbell low (hit, tip, -)
    [56, 100, 56],   # [3]  Cowbell medium (hit, tip, -)
    [102, 103, 102], # [4]  Cowbell high (hit, tip, -)
    [43, 43, 43],    # [5]  Tom very low (hit, -, -)
    [45, 45, 45],    # [6]  Tom low (hit, -, -)
    [47, 47, 47],    # [7]  Tom medium (hit, -, -)
    [48, 48, 48],    # [8]  Tom high (hit, -, -)
    [50, 50, 50],    # [9]  Tom very high (hit, -, -)
    [42, 92, 46],    # [10] Hihat (closed, half, open)
    [44, 44, 44],    # [11] Pedal hihat (hit, -, -)
    [57, 98, 57],    # [12] Crash medium (hit, choke, -)
    [49, 97, 49],    # [13] Crash high (hit, choke, -)
    [55, 95, 55],    # [14] Splash (hit, choke, -)
    [51, 93, 127],   # [15] Ride (middle, edge, bell)
    [52, 96, 52],    # [16] China (hit, choke, -)
]

# Articulation IDs that differ from their MIDI output note.
# Most IDs equal the MIDI note; only the non-standard ones are listed here.
# Source: alphaTab InstrumentArticulation.create(uniqueId, name, staffLine, outputMidi, ...)
_ART_TO_MIDI: dict[int, int] = {
    91: 38,   # Snare rim shot       -> snare (38)
    92: 46,   # Hihat half-open      -> open hihat (46)
    93: 51,   # Ride edge            -> ride (51)
    94: 51,   # Ride choke           -> ride (51)
    95: 55,   # Splash choke         -> splash (55)
    96: 52,   # China choke          -> china (52)
    97: 49,   # Crash high choke     -> crash (49)
    98: 57,   # Crash medium choke   -> crash 2 (57)
    99: 56,   # Cowbell low hit      -> cowbell (56)
    100: 56,  # Cowbell low tip      -> cowbell (56)
    101: 56,  # Cowbell medium tip   -> cowbell (56)
    102: 56,  # Cowbell high hit     -> cowbell (56)
    103: 56,  # Cowbell high tip     -> cowbell (56)
    104: 60,  # Bongo high mute      -> bongo high (60)
    105: 60,  # Bongo high slap      -> bongo high (60)
    106: 61,  # Bongo low mute       -> bongo low (61)
    107: 61,  # Bongo low slap       -> bongo low (61)
    108: 64,  # Conga low slap       -> conga low (64)
    109: 64,  # Conga low mute       -> conga low (64)
    110: 63,  # Conga high slap      -> conga high (63)
    111: 54,  # Tambourine return    -> tambourine (54)
    112: 54,  # Tambourine roll      -> tambourine (54)
    113: 54,  # Tambourine hand      -> tambourine (54)
    114: 43,  # Grancassa            -> tom very low (43)
    115: 49,  # Piatti hit           -> crash (49)
    116: 49,  # Piatti hand          -> crash (49)
    117: 69,  # Cabasa return        -> cabasa (69)
    118: 70,  # Left maraca return   -> maraca (70)
    119: 70,  # Right maraca hit     -> maraca (70)
    120: 70,  # Right maraca return  -> maraca (70)
    122: 82,  # Shaker return        -> shaker (82)
    123: 53,  # Bell tree return     -> ride bell (53)
    124: 62,  # Golpe thumb          -> conga high mute (62)
    125: 62,  # Golpe finger         -> conga high mute (62)
    126: 59,  # Ride cymbal 2 mid    -> ride 2 (59)
    127: 59,  # Ride bell            -> ride 2 (59)
}

# GP5 special fret->MIDI overrides (only 5 non-GM entries; all others are fret==MIDI)
# Source: alphaTab Gp3To5Importer._gp5PercussionInstrumentMap
_GP5_SPECIAL: dict[int, int] = {27: 42, 28: 60, 29: 29, 30: 30, 32: 31}


def _gp6_element_variation_to_midi(element: int, variation: int) -> int | None:
    """
    Convert a GP6 Element+Variation pair to a MIDI percussion note number.

    This is the primary drum encoding in all GPX (Guitar Pro 6) files.
    Ported from alphaTab PercussionMapper.articulationFromElementVariation().
    """
    if element < 0 or element >= len(_GP6_EV):
        return None  # Unknown element — silently skip
    var = min(max(variation, 0), len(_GP6_EV[element]) - 1)
    art_id = _GP6_EV[element][var]
    return _ART_TO_MIDI.get(art_id, art_id)


def _note_midi(note_el: ET.Element, string_pitches: list[int]) -> int | None:
    """
    Extract MIDI note number from a GPIF <Note> element.

    Handles all encoding variants found in Guitar Pro files:

    1. Element + Variation  (GPX / GP6 drums — primary encoding)
       All GPX percussion tracks use this. The Element index selects the drum
       piece; Variation selects the articulation (e.g. open vs closed hi-hat).

    2. String + Fret  (guitar/bass, and some GPX pitched tracks)
       String is 0-based from the highest string. Pitch = string_pitches[idx] + fret.
       Also used for guitar-model vocal tracks and some piano tabs (string_pitches
       must be present and in descending MIDI order, high string first).

    3. Tone + Octave  (GPX melodic/piano tracks — diatonic step encoding)
       Step is an integer 0–6 (C=0 … B=6). MIDI = (octave+1)*12 + semitone.

    4. InstrumentArticulation index  (GP7 primary encoding, rare in GPX)
       A direct index into the track's percussionArticulations list. Not used
       in GP6 files; handled here as a best-effort fallback using the standard
       GM percussion table.
    """
    props = {p.get('name'): p for p in note_el.findall('.//Property')}

    # ── Encoding 1: Element + Variation (GPX drums) ──────────────────────
    if 'Element' in props:
        try:
            element = int(props['Element'].findtext('Element') or 0)
            variation = int((props.get('Variation') or ET.Element('x')).findtext('Variation') or 0)
            return _gp6_element_variation_to_midi(element, variation)
        except (ValueError, TypeError):
            return None

    # ── Encoding 2: String + Fret (guitar/bass/vocal/some piano) ─────────
    if 'String' in props and 'Fret' in props:
        try:
            str_idx = int(props['String'].findtext('String') or 0)
            fret = int(props['Fret'].findtext('Fret') or 0)
            if string_pitches:
                # GP6 String index 0 = highest string, and string_pitches is
                # stored high→low (index 0 = highest) — the same ordering
                # _gpx_tuning relies on. So index directly by str_idx; an
                # earlier reverse (n-1-str_idx) transposed every String+Fret
                # note to the wrong string's pitch.
                if 0 <= str_idx < len(string_pitches):
                    return string_pitches[str_idx] + fret
            return None
        except (ValueError, TypeError):
            return None

    # ── Encoding 3: Tone + Octave (GPX melodic/piano) ────────────────────
    if 'Tone' in props and 'Octave' in props:
        try:
            step = int(props['Tone'].findtext('Step') or 0)
            octave = int(props['Octave'].findtext('Number') or 4)
            semi = _STEP_TO_SEMI[step % 7]
            return (octave + 1) * 12 + semi  # C4 = MIDI 60
        except (ValueError, TypeError, IndexError):
            return None

    # ── Encoding 4: InstrumentArticulation index (GP7 fallback) ──────────
    if 'InstrumentArticulation' in props:
        try:
            art_id = int(props['InstrumentArticulation'].findtext('InstrumentArticulation') or 0)
            # art_id is usually the MIDI note number directly for standard GM kit
            return _ART_TO_MIDI.get(art_id, art_id)
        except (ValueError, TypeError):
            return None

    return None


def _note_is_tie(note_el: ET.Element) -> bool:
    """True if this note is a tied continuation (destination tie)."""
    tie = note_el.find('Tie')
    if tie is None:
        return False
    # GP6 XML: <Tie origin="true"> on the first note, <Tie destination="true"> on the tied
    return tie.get('destination', '').lower() in ('true', '1')


# ---------------------------------------------------------------------------
# list_tracks — mirrors gp2rs.list_tracks interface
# ---------------------------------------------------------------------------

def list_tracks(gp_path: str) -> list[dict]:
    """List all tracks in a .gpx file with basic info for the editor UI."""
    root = _load_gpif(gp_path)
    tracks = _gpif_tracks(root)
    # Note counts are computed once in _gpif_tracks ('note_count'); reuse them
    # here instead of walking the bar/voice/beat graph a second time.

    result = []
    for i, t in enumerate(tracks):
        is_bass = bool(
            (
                not t['is_drums']
                and not t['string_pitches']  # no string tuning = not guitar-family
                and 32 <= t['midi_program'] <= 39
            ) or (
                t['string_pitches']
                and max(t['string_pitches']) <= 48  # bass top string ≤ C3
            )
        )
        is_piano = (
            not t['is_drums']
            and not t['string_pitches']
            and t['midi_program'] in set(range(0, 8)) | set(range(16, 24)) | {80, 81, 82, 83}
        ) or (
            not t['is_drums']
            and any(kw in t['name'].lower() for kw in ('piano', 'keys', 'keyboard', 'organ'))
        )

        is_vocal = _is_vocal_track(t)

        result.append({
            'index': i,
            'name': t['name'],
            'strings': len(t['string_pitches']),
            'is_percussion': t['is_drums'],
            'is_piano': is_piano,
            'is_drums': t['is_drums'],
            'is_bass': is_bass,
            'is_vocal': is_vocal,
            'instrument': t['midi_program'],
            'notes': t['note_count'],
        })
    return result


# ---------------------------------------------------------------------------
# convert_file — mirrors gp2rs.convert_file interface
# Converts GPX tracks directly to Rocksmith XML, reusing gp2rs._build_xml
# ---------------------------------------------------------------------------

def convert_file(
    gp_path: str,
    output_dir: str,
    track_indices: list[int] | None = None,
    audio_offset: float = 0.0,
    arrangement_names: dict[int, str] | None = None,
    force_standard_tuning: bool = False,
    *,
    expand_repeats: bool = True,
) -> list[str]:
    """Convert a .gpx file to Rocksmith XML arrangement files.

    Mirrors gp2rs.convert_file so the editor plugin can call it transparently.
    expand_repeats is accepted for API compatibility but repeat expansion from
    GPX XML is not yet implemented (the GPIF repeat markup differs from GP5
    binary and requires a separate walker — planned for a follow-up PR).
    """
    root = _load_gpif(gp_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Metadata
    score = root.find('Score')
    title = (score.findtext('Title') or '').strip() if score is not None else ''
    artist = (score.findtext('Artist') or '').strip() if score is not None else ''
    album = (score.findtext('Album') or '').strip() if score is not None else ''
    tempo_bpm = _gpif_tempo(root)  # initial tempo (used as fallback)
    tempo_map = _build_tempo_map(root)  # full bar->bpm map for multi-tempo songs

    tracks = _gpif_tracks(root)

    if track_indices is None:
        track_indices, auto_names = _auto_select_gpx(tracks)
        if not arrangement_names:
            arrangement_names = auto_names

    names = arrangement_names or {}

    # Pre-build lookup tables (shared across all tracks)
    masterbars = list(root.find('MasterBars') or [])

    # expand_repeats is accepted for gp2rs.convert_file parity but GPIF repeat
    # expansion (Repeat / AlternateEndings / Directions) is not implemented yet.
    # Surface that to the caller rather than only the docstring: if the score
    # actually uses repeats, the produced bar count/timing will differ from the
    # equivalent .gp5. Warn once so plugin code/logs don't silently drift.
    if expand_repeats and any(
        mb.find('Repeat') is not None or mb.find('AlternateEndings') is not None
        for mb in masterbars
    ):
        _log.warning(
            "GPX '%s' contains repeat/volta markup but GPX repeat expansion is "
            "not implemented; arrangement is emitted single-pass and may not "
            "match the equivalent .gp5.", gp_path
        )
    bars_list = list(root.find('Bars') or [])
    bars_by_id = {b.get('id'): b for b in bars_list}
    voices_dict = {v.get('id'): v for v in (root.find('Voices') or [])}
    beats_dict = {b.get('id'): b for b in (root.find('Beats') or [])}
    notes_dict = {n.get('id'): n for n in (root.find('Notes') or [])}
    rhythms_dict = {r.get('id'): r for r in (root.find('Rhythms') or [])}

    # Map filtered track index -> raw track index (needed for bar lookup)
    raw_tracks = list(root.find('Tracks') or [])
    filtered_to_raw: dict[int, int] = {}
    filtered_pos = 0
    for raw_idx, t_el in enumerate(raw_tracks):
        name = (t_el.findtext('Name') or '').strip()
        if name.startswith('@$') and name.endswith('$@'):
            continue
        filtered_to_raw[filtered_pos] = raw_idx
        filtered_pos += 1

    output_files = []

    for track_idx in track_indices:
        if track_idx >= len(tracks):
            continue
        track = tracks[track_idx]
        arr_name = names.get(track_idx, '')
        raw_idx = filtered_to_raw.get(track_idx, track_idx)

        # Decide conversion mode
        is_drum = track['is_drums'] or (arr_name.lower().startswith('drums'))
        is_vocal = _is_vocal_track(track) or arr_name.lower().startswith('vocal')
        is_keys = (
            not is_drum and not is_vocal and not track['string_pitches']
            and (
                track['midi_program'] in set(range(0, 8)) | set(range(16, 24)) | {80, 81, 82, 83}
                or any(kw in track['name'].lower() for kw in ('piano', 'keys', 'organ'))
                or arr_name.lower().startswith('keys')
            )
        )

        # Vocal tracks get their own converter — outputs vocals XML, not notes XML
        if is_vocal:
            xml_str = convert_vocal_track(
                root, track, raw_idx,
                masterbars, bars_by_id, voices_dict, beats_dict, notes_dict, rhythms_dict,
                title=title, artist=artist, album=album, tempo_bpm=tempo_bpm,
                audio_offset=audio_offset, arr_name=arr_name or 'Vocals',
            )
            filename = f"{_safe_filename_stem(track['name'])}_{arr_name or 'Vocals'}.xml"
            filepath = safe_join(out, filename)
            if filepath is None:
                raise ValueError(f"unsafe output filename from track name: {track['name']!r}")
            filepath.write_text(xml_str)
            output_files.append(str(filepath))
            continue

        # Iterate all masterbars and collect notes for this track
        from gp2rs import RsNote, RsBeat, RsSection, RsAnchor, ChordTemplate, RsChord, _build_xml

        rs_notes: list[RsNote] = []
        rs_chords: list[RsChord] = []
        chord_templates: list[ChordTemplate] = []
        chord_template_map: dict[tuple, int] = {}
        beats_out: list[RsBeat] = []
        sections: list[RsSection] = []
        section_counts: dict[str, int] = {}
        last_note_per_key: dict = {}

        current_time = 0.0
        num_raw_tracks = len(raw_tracks)

        # Resolve current tempo per bar from the tempo map
        _tempo_iter = iter(tempo_map)
        _next_tempo_bar, _next_tempo_bpm = next(_tempo_iter, (999999, tempo_bpm))
        _cur_tempo = tempo_bpm

        for mb_idx_loop, mb in enumerate(masterbars):
            # Advance tempo map
            while mb_idx_loop >= _next_tempo_bar:
                _cur_tempo = _next_tempo_bpm
                _next_tempo_bar, _next_tempo_bpm = next(_tempo_iter, (999999, _cur_tempo))

            time_sig = mb.findtext('Time', '4/4')
            try:
                num_b, den_b = [int(x) for x in time_sig.split('/')]
            except ValueError:
                num_b, den_b = 4, 4
            beats_per_bar = num_b * (4.0 / den_b)
            bar_duration = beats_per_bar * (60.0 / _cur_tempo)
            # One beat = one denominator unit, NOT always a quarter note. For
            # 6/8, 3/8, 12/8 etc. the beat is shorter than a quarter, so scale
            # by (4/den_b); otherwise markers spread at quarter spacing and run
            # past the bar end on compound/non-quarter meters.
            beat_len = (60.0 / _cur_tempo) * (4.0 / den_b)

            # Downbeat
            beats_out.append(RsBeat(time=current_time + audio_offset, measure=-999))  # placeholder
            for sub_b in range(1, num_b):
                beats_out.append(RsBeat(
                    time=current_time + sub_b * beat_len + audio_offset,
                    measure=-1,
                ))

            # Section markers
            section_el = mb.find('Section')
            if section_el is not None:
                text = (section_el.findtext('Text') or '').strip()
                if text:
                    sname = text.lower().replace(' ', '')
                    section_counts[sname] = section_counts.get(sname, 0) + 1
                    sections.append(RsSection(
                        name=sname,
                        time=current_time + audio_offset,
                        number=section_counts[sname],
                    ))

            # Get this track's bar
            bar_ids = mb.findtext('Bars', '').split()
            bid = bar_ids[raw_idx] if raw_idx < len(bar_ids) else '-1'

            if bid != '-1' and bid:
                bar = bars_by_id.get(bid)
                if bar is not None:
                    for vid in bar.findtext('Voices', '').split():
                        if vid == '-1':
                            continue
                        voice = voices_dict.get(vid)
                        if voice is None:
                            continue

                        voice_time = current_time
                        for beat_id in voice.findtext('Beats', '').split():
                            beat_el = beats_dict.get(beat_id)
                            if beat_el is None:
                                continue

                            dur = _beat_dur_secs(beat_el, rhythms_dict, _cur_tempo)
                            t = voice_time + audio_offset

                            notes_text = beat_el.findtext('Notes', '').strip()
                            if notes_text:
                                beat_note_els = [
                                    notes_dict[nid]
                                    for nid in notes_text.split()
                                    if nid in notes_dict
                                ]

                                beat_rs_notes = []
                                for note_el in beat_note_els:
                                    if _note_is_tie(note_el):
                                        # Extend sustain of previous note at same pitch
                                        midi = _note_midi(note_el, track['string_pitches'])
                                        if midi is not None:
                                            prev = last_note_per_key.get(midi)
                                            if prev is not None and prev.time < t:
                                                prev.sustain = max(prev.sustain, (t + dur) - prev.time)
                                        continue

                                    midi = _note_midi(note_el, track['string_pitches'])
                                    if midi is None:
                                        continue

                                    if is_drum:
                                        # GP6 decodes drum pieces to their real GM
                                        # MIDI numbers directly (splash 55, china
                                        # 52, ride bell 53, pedal hi-hat 44, …), so
                                        # no GM_DRUM_MAP gate is needed — that gate
                                        # only exists on the GP3-5 path because
                                        # pyguitarpro emits raw MIDI. The generic
                                        # midi -> (string, fret) encoding below
                                        # represents any drum piece, so filtering
                                        # here would silently drop valid pieces.
                                        rs_str = midi // 24
                                        rs_fret = midi % 24
                                    elif is_keys:
                                        rs_str = midi // 24
                                        rs_fret = midi % 24
                                    else:
                                        # Guitar/bass: need string + fret from XML
                                        props = {p.get('name'): p for p in note_el.findall('.//Property')}
                                        if 'String' in props and 'Fret' in props:
                                            try:
                                                gp_str = int(props['String'].findtext('String') or 0)
                                                fret = int(props['Fret'].findtext('Fret') or 0)
                                                num_strings = len(track['string_pitches'])
                                                # GP6 String is 0-based; RS string is low=0
                                                # GP6 String 0 = highest string
                                                rs_str = num_strings - 1 - gp_str if num_strings > 0 else gp_str
                                                rs_fret = fret
                                            except (ValueError, TypeError):
                                                continue
                                        else:
                                            continue

                                    sustain = dur if dur > 0.2 else 0.0
                                    if is_drum:
                                        sustain = 0.0

                                    rn = RsNote(
                                        time=t,
                                        string=rs_str,
                                        fret=rs_fret,
                                        sustain=sustain,
                                    )

                                    # Techniques from note effects
                                    if note_el.find('LetRing') is not None:
                                        rn.link_next = True
                                    if note_el.find('Vibrato') is not None:
                                        rn.vibrato = True
                                    if note_el.find('Accent') is not None:
                                        rn.accent = True
                                    if note_el.find('LeftHandTapping') is not None:
                                        rn.tap = True
                                    if note_el.find('PalmMute') is not None:
                                        rn.palm_mute = True

                                    beat_rs_notes.append(rn)
                                    last_note_per_key[midi] = rn

                                if len(beat_rs_notes) == 1:
                                    rs_notes.append(beat_rs_notes[0])
                                elif len(beat_rs_notes) > 1:
                                    width = max(6, max(n.string for n in beat_rs_notes) + 1)
                                    frets_t = [-1] * width
                                    for n in beat_rs_notes:
                                        if 0 <= n.string < width:
                                            frets_t[n.string] = n.fret
                                    fkey = tuple(frets_t)
                                    if fkey not in chord_template_map:
                                        chord_template_map[fkey] = len(chord_templates)
                                        chord_templates.append(ChordTemplate(
                                            name='', frets=list(frets_t), fingers=[-1] * width,
                                        ))
                                    rs_chords.append(RsChord(
                                        time=t,
                                        template_idx=chord_template_map[fkey],
                                        notes=beat_rs_notes,
                                    ))

                            voice_time += dur

            current_time += bar_duration

        # Fix downbeat measure numbers
        bar_num = 1
        for b in beats_out:
            if b.measure == -999:
                b.measure = bar_num
                bar_num += 1

        if not sections:
            sections.append(RsSection(name='default', time=audio_offset, number=1))

        rs_notes.sort(key=lambda n: n.time)
        rs_chords.sort(key=lambda c: c.time)

        # Anchors
        if is_drum or is_keys:
            anchors = [RsAnchor(time=audio_offset, fret=1, width=24)]
        else:
            all_frets = [(n.time, n.fret) for n in rs_notes if n.fret > 0]
            for c in rs_chords:
                for cn in c.notes:
                    if cn.fret > 0:
                        all_frets.append((cn.time, cn.fret))
            all_frets.sort()
            first_fret = all_frets[0][1] if all_frets else 1
            anchors = [RsAnchor(time=audio_offset, fret=max(1, first_fret - 1), width=4)]
            for t_f, fret in all_frets:
                lo = anchors[-1].fret
                if fret < lo or fret > lo + anchors[-1].width:
                    new_f = max(1, fret - 1)
                    if new_f != anchors[-1].fret:
                        anchors.append(RsAnchor(time=t_f, fret=new_f, width=4))

        song_length = current_time + audio_offset

        # Use the track's actual string count (matches gp2rs: len(track.strings)).
        # Forcing a minimum of 6 emitted 4/5-string bass as a 6-string
        # arrangement with mismatched tuning/string indexing. Guitar-family
        # tracks always have string_pitches (that's how they're classified).
        # Guitar-family tracks have string_pitches; but an explicit track_indices
        # entry can route a program-only track (no tuning, not drum/keys/vocal)
        # here — fall back to a 6-string default so num_strings/tuning are never
        # empty (which would emit invalid arrangement metadata via _build_xml).
        num_strings = 6 if (is_drum or is_keys) else (len(track['string_pitches']) or 6)
        # force_standard_tuning parity with gp2rs.convert_file: E standard
        # (all-zero offsets), frets unchanged. Drums/keys are always [0]*6.
        if is_drum or is_keys or force_standard_tuning:
            tuning = [0] * num_strings
        else:
            tuning = _gpx_tuning(track)

        xml_str = _build_xml(
            title=title or 'Untitled',
            artist=artist or 'Unknown',
            album=album or '',
            year='',
            arrangement=arr_name or ('Drums' if is_drum else ('Keys' if is_keys else 'Lead')),
            tuning=tuning,
            num_strings=num_strings,
            song_length=song_length,
            audio_offset=audio_offset,
            beats=beats_out,
            sections=sections,
            notes=rs_notes,
            chords=rs_chords,
            chord_templates=chord_templates,
            anchors=anchors,
            tempo=int(tempo_bpm),
        )

        filename = f"{_safe_filename_stem(track['name'])}_{arr_name or 'arr'}.xml"
        filepath = safe_join(out, filename)
        if filepath is None:
            raise ValueError(f"unsafe output filename from track name: {track['name']!r}")
        filepath.write_text(xml_str)
        output_files.append(str(filepath))

    return output_files



# ---------------------------------------------------------------------------
# Vocal track detection and conversion
# ---------------------------------------------------------------------------

# MIDI programs associated with voice / choir / lead synth used for vocals in GP tabs
_VOCAL_MIDI_PROGRAMS = {52, 53, 54, 85, 86, 87}  # Choir Aahs, Voice Oohs, Synth Voice, Lead Voice
_VOCAL_NAME_KEYWORDS = {'vocal', 'voice', 'vox', 'sing', 'lyric', 'choir', 'lead voc', 'backing voc'}


def _is_vocal_track(track: dict) -> bool:
    """Return True if this track looks like a vocal/lyric part."""
    name_l = track['name'].lower()
    if any(kw in name_l for kw in _VOCAL_NAME_KEYWORDS):
        return True
    if track['midi_program'] in _VOCAL_MIDI_PROGRAMS and not track['is_drums']:
        return True
    return False


def _gpx_lyric_to_rs(raw: str) -> str:
    """
    Convert a GPX lyric token to Rocksmith vocal lyric format.

    GPX encodes syllable continuation with a trailing hyphen (e.g. "in-", "t-").
    Rocksmith uses the same convention for mid-word syllables.  For word-final
    syllables with no hyphen, RS requires a "+" suffix to signal "connect to
    next syllable without a space" — but only when the next beat is a
    continuation of the same word.  We handle this at the sequence level in
    convert_vocal_track() rather than per-token, so here we just normalise
    whitespace and pass through.

    Special GPX tokens:
        "+"  (rare) — word joiner used in some tabs; map to RS "+"
        " "  (empty/space) — rest beat with no lyric; skip at caller
    """
    raw = raw.strip()
    if not raw:
        return ''
    # GP sometimes uses "+" as an explicit word-joiner; keep it
    return raw


def convert_vocal_track(
    root: ET.Element,
    track: dict,
    raw_idx: int,
    masterbars: list,
    bars_by_id: dict,
    voices_dict: dict,
    beats_dict: dict,
    notes_dict: dict,
    rhythms_dict: dict,
    *,
    title: str = '',
    artist: str = '',
    album: str = '',
    tempo_bpm: float = 120.0,
    audio_offset: float = 0.0,
    arr_name: str = 'Vocals',
) -> str:
    """
    Convert a GPX vocal track to a Rocksmith 2014 vocals arrangement XML.

    Each beat with a lyric and a note becomes a <vocal> element:
        time   — seconds from song start + audio_offset
        note   — MIDI pitch (raw String+Fret value, no transposition applied;
                 see module docstring for a note on vocal transposition)
        length — duration in seconds (ties extend this)
        lyric  — syllable text, RS-formatted:
                   trailing "-" = mid-word hyphen (same as GPX)
                   trailing "+" = connect to next token (no space, no hyphen)
                   no suffix    = word end (RS inserts a space before next)

    Beats with a lyric but no pitch note are included as pitch-0 rests so the
    display timeline stays intact.  Beats with no lyric are skipped entirely.

    The output is a minimal but valid Rocksmith vocals XML.  It does not include
    ebeats or phrases (RS parses vocal XMLs without them).
    """
    string_pitches = track['string_pitches']  # high→low, standard guitar if vocal

    # ---------------------------------------------------------------------------
    # Pass 1: collect raw (time, duration, lyric, midi_note) tuples
    # ---------------------------------------------------------------------------
    raw_vocals: list[dict] = []   # {time, length, lyric, note, is_tie_origin}
    current_time = 0.0

    # Build per-bar tempo map for vocal timing (handles tempo changes correctly)
    _v_tempo_map = _build_tempo_map(root)
    _v_tempo_iter = iter(_v_tempo_map)
    _v_next_bar, _v_next_bpm = next(_v_tempo_iter, (999999, tempo_bpm))
    _v_cur_tempo = tempo_bpm

    for _v_mb_idx, mb in enumerate(masterbars):
        # Advance tempo
        while _v_mb_idx >= _v_next_bar:
            _v_cur_tempo = _v_next_bpm
            _v_next_bar, _v_next_bpm = next(_v_tempo_iter, (999999, _v_cur_tempo))

        time_sig = mb.findtext('Time', '4/4')
        try:
            num_b, den_b = [int(x) for x in time_sig.split('/')]
        except ValueError:
            num_b, den_b = 4, 4
        bar_duration = num_b * (4.0 / den_b) * (60.0 / _v_cur_tempo)

        bar_ids = mb.findtext('Bars', '').split()
        bid = bar_ids[raw_idx] if raw_idx < len(bar_ids) else '-1'

        if bid != '-1' and bid:
            bar = bars_by_id.get(bid)
            if bar is not None:
                for vid in bar.findtext('Voices', '').split():
                    if vid == '-1':
                        continue
                    voice = voices_dict.get(vid)
                    if voice is None:
                        continue

                    voice_time = current_time
                    for beat_id in voice.findtext('Beats', '').split():
                        beat_el = beats_dict.get(beat_id)
                        if beat_el is None:
                            continue

                        dur = _beat_dur_secs(beat_el, rhythms_dict, _v_cur_tempo)

                        # Extract lyric from beat
                        lyric_el = beat_el.find('Lyrics')
                        lyric_raw = ''
                        if lyric_el is not None:
                            line = lyric_el.find('Line')
                            if line is not None and line.text:
                                lyric_raw = line.text.strip()

                        # Extract pitch from note(s) on this beat
                        midi_note = 0
                        is_tie_origin = False
                        notes_text = beat_el.findtext('Notes', '').strip()
                        if notes_text:
                            for nid in notes_text.split():
                                note_el = notes_dict.get(nid)
                                if note_el is None:
                                    continue

                                # Tie destination: extend previous vocal's length
                                if _note_is_tie(note_el):
                                    if raw_vocals:
                                        raw_vocals[-1]['length'] = max(
                                            raw_vocals[-1]['length'],
                                            (voice_time + audio_offset + dur) - raw_vocals[-1]['time']
                                        )
                                    # Do NOT advance voice_time here — the
                                    # beat-end `voice_time += dur` below advances
                                    # exactly once per beat. Incrementing here too
                                    # double-advanced tied beats and drifted every
                                    # later vocal event.
                                    continue

                                # Tie origin: note continues into next beat
                                tie = note_el.find('Tie')
                                if tie is not None and tie.get('origin', '').lower() in ('true', '1'):
                                    is_tie_origin = True

                                midi = _note_midi(note_el, string_pitches)
                                if midi is not None:
                                    midi_note = midi
                                break  # one pitch per vocal beat

                        # Only emit if there is a lyric (beats without lyrics are rests)
                        if lyric_raw:
                            lyric = _gpx_lyric_to_rs(lyric_raw)
                            if lyric:
                                raw_vocals.append({
                                    'time': round(voice_time + audio_offset, 3),
                                    'length': round(dur, 3),
                                    'lyric': lyric,
                                    'note': midi_note,
                                    'is_tie_origin': is_tie_origin,
                                })

                        voice_time += dur

        current_time += bar_duration

    song_length = current_time + audio_offset

    # ---------------------------------------------------------------------------
    # Pass 2: apply RS lyric suffix convention
    #
    # GPX already marks mid-word syllables with trailing "-" (e.g. "in-", "e-").
    # RS additionally requires "+" on the last syllable of a word when the next
    # syllable is a direct continuation with no space.  In GPX this is implicitly
    # signalled by the next token having a leading lowercase letter (continuation)
    # vs an uppercase or punctuation-leading token (new word).  We use a simpler
    # heuristic: if this token ends with "-" it is already marked; otherwise it
    # is a word-end and needs no extra suffix (RS treats no-suffix as word-end).
    #
    # The one GPX convention we need to convert: some tabs use "+" as an explicit
    # join (no hyphen, no space).  These are passed through unchanged.
    # ---------------------------------------------------------------------------
    # (No transformation needed beyond what _gpx_lyric_to_rs already does —
    #  GPX "-" maps directly to RS "-" for mid-word breaks, and word-final tokens
    #  with no suffix are correct as-is.  No "+" insertion is required.)

    # ---------------------------------------------------------------------------
    # Pass 3: emit XML
    # ---------------------------------------------------------------------------
    return _build_vocals_xml(
        title=title or 'Untitled',
        artist=artist or 'Unknown',
        album=album or '',
        arrangement=arr_name,
        song_length=song_length,
        audio_offset=audio_offset,
        vocals=raw_vocals,
        tempo=int(tempo_bpm),
    )


def _build_vocals_xml(
    title: str,
    artist: str,
    album: str,
    arrangement: str,
    song_length: float,
    audio_offset: float,
    vocals: list[dict],
    tempo: int,
) -> str:
    """Build a Rocksmith 2014 vocals arrangement XML string."""
    from xml.dom import minidom

    # Rocksmith 2014 vocals arrangement is a flat <vocals> document — NOT a
    # <song> wrapper. Every lyric consumer in the codebase keys off the root
    # tag being literally "vocals" (lib/sloppak_convert.py::_parse_lyrics,
    # lib/loosefolder.py, server.py highway loader), so a <song> root would be
    # silently skipped and the generated lyrics never loaded. The schema is
    # just <vocals count="N"> with <vocal time note length lyric/> children;
    # the song-level metadata params are not part of it.
    root = ET.Element('vocals', count=str(len(vocals)))
    for v in vocals:
        ET.SubElement(root, 'vocal',
                      time=f"{v['time']:.3f}",
                      note=str(v['note']),
                      length=f"{v['length']:.3f}",
                      lyric=v['lyric'])

    xml_str = ET.tostring(root, encoding='unicode')
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent='  ', encoding=None)


def _gpx_tuning(track: dict) -> list[int]:
    """Compute RS tuning offsets (semitones from standard) from GPX string pitches."""
    from gp2rs import STANDARD_TUNING_GUITAR, STANDARD_TUNING_BASS
    pitches = track['string_pitches']
    n = len(pitches)
    if n == 0:
        return [0] * 6

    is_bass = max(pitches) <= 48
    if is_bass:
        # GPX pitches are high->low, so max(pitches) is the top string. 5-string
        # bass comes in two standards: high-C (C G D A E, top MIDI 48) and low-B
        # (G D A E B, top MIDI 43). Mirror gp2rs._standard_tuning_for: midpoint
        # is 45.5, so top >= 46 selects high-C (drop the low string from the
        # 6-string table); otherwise 4-string / low-B 5-string skips the high C.
        top_midi = max(pitches)
        if n == 5 and top_midi >= 46:
            standard = STANDARD_TUNING_BASS[:5]
        elif n <= 5:
            standard = STANDARD_TUNING_BASS[1:1 + n]
        else:
            standard = STANDARD_TUNING_BASS[:n]
    else:
        standard = STANDARD_TUNING_GUITAR[:n]

    # GPX pitches are high→low (index 0 = highest string)
    # RS tuning is low→high (index 0 = lowest string)
    offsets = [0] * n
    for gp_idx, pitch in enumerate(pitches):
        rs_idx = n - 1 - gp_idx
        if gp_idx < len(standard):
            offsets[rs_idx] = pitch - standard[gp_idx]
    return offsets


def _auto_select_gpx(tracks: list[dict]) -> tuple[list[int], dict[int, str]]:
    """Auto-select guitar/bass/keys/drums tracks and assign arrangement names."""
    GUITAR_PROGS = set(range(24, 32))
    BASS_PROGS = set(range(32, 40))
    KEYS_PROGS = set(range(0, 8)) | set(range(16, 24)) | {80, 81, 82, 83}
    SKIP_NAMES = {'string', 'choir', 'brass', 'flute', 'violin', 'cello', 'horn'}

    selected = []
    for i, t in enumerate(tracks):
        # Skip note-empty tracks (placeholder / muted-empty) so auto-selection
        # doesn't emit unusable empty arrangements — matches the non-GPX path.
        if t.get('note_count', 1) == 0:
            continue
        if t['is_drums']:
            selected.append((i, 'drums'))
            continue

        name_l = t['name'].lower()

        if _is_vocal_track(t):
            selected.append((i, 'vocal'))
            continue

        if any(kw in name_l for kw in SKIP_NAMES) and not t['string_pitches']:
            continue

        is_bass = (t['string_pitches'] and max(t['string_pitches']) <= 48) \
            or t['midi_program'] in BASS_PROGS
        is_guitar = bool(t['string_pitches']) and not is_bass
        is_keys = (not t['string_pitches'] and t['midi_program'] in KEYS_PROGS) \
            or any(kw in name_l for kw in ('piano', 'keys', 'organ'))

        if is_bass:
            selected.append((i, 'bass'))
        elif is_guitar:
            selected.append((i, 'guitar'))
        elif is_keys:
            selected.append((i, 'keys'))

    if not selected:
        for i, t in enumerate(tracks):
            if not t['is_drums'] and t.get('note_count', 1) > 0:
                selected.append((i, 'guitar'))

    indices = []
    name_map = {}
    counts: dict[str, int] = {}
    RS_NAMES = {'guitar': ('Lead', 'Rhythm', 'Combo'), 'bass': ('Bass',), 'keys': ('Keys',), 'drums': ('Drums',), 'vocal': ('Vocals',)}

    for idx, role in selected:
        counts[role] = counts.get(role, 0) + 1
        c = counts[role]
        names_for_role = RS_NAMES.get(role, (role.title(),))
        arr_name = names_for_role[min(c - 1, len(names_for_role) - 1)]
        if c > len(names_for_role):
            arr_name = f"{names_for_role[-1]} {c}"
        indices.append(idx)
        name_map[idx] = arr_name

    return indices, name_map
