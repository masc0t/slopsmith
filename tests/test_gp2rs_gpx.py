"""Tests for lib/gp2rs_gpx.py — the Guitar Pro 6 (.gpx) import path.

Fixture-free: every test exercises a pure helper with hand-built inputs
(ElementTree fragments, tuning lists, crafted container headers). The binary
BCFZ/BCFS round-trip needs a real .gpx and is covered by manual validation in
the PR; here we pin the input-validation guards and the conversion helpers
that are easy to drive without a fixture.
"""

import struct
import xml.etree.ElementTree as ET

import pytest

from gp2rs_gpx import (
    _decompress_bcfz,
    _parse_bcfs,
    _safe_filename_stem,
    _note_is_tie,
    _gpx_tuning,
    _gp6_element_variation_to_midi,
    _GPX_MAX_DECOMPRESSED,
)


# ── _safe_filename_stem ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name, expected", [
    ("Lead Guitar", "Lead_Guitar"),
    ("AC/DC", "AC_DC"),
    (r"..\..\evil", "evil"),
    ("../../etc/passwd", "etc_passwd"),
    ("C:\\Windows\\x", "C_Windows_x"),
    ("", "track"),
    ("...", "track"),
    ("Bass (5)", "Bass_5"),
])
def test_safe_filename_stem(name, expected):
    out = _safe_filename_stem(name)
    assert out == expected
    # Never contains a path separator or traversal segment.
    assert "/" not in out and "\\" not in out
    assert ".." not in out


# ── _decompress_bcfz / _parse_bcfs input guards ─────────────────────────────

def test_decompress_bcfz_rejects_bad_magic():
    with pytest.raises(ValueError):
        _decompress_bcfz(b"XXXX" + b"\x00" * 8)


def test_decompress_bcfz_rejects_oversized_declared_size():
    # 4 bytes after the magic are read verbatim as a little-endian uint32 = the
    # declared decompressed size. Declare > cap -> ValueError before allocating.
    blob = b"BCFZ" + struct.pack("<I", _GPX_MAX_DECOMPRESSED + 1)
    with pytest.raises(ValueError):
        _decompress_bcfz(blob)


def test_parse_bcfs_rejects_bad_magic():
    with pytest.raises(ValueError):
        _parse_bcfs(b"NOPE" + b"\x00" * 16)


# ── _note_is_tie ────────────────────────────────────────────────────────────

def test_note_is_tie_destination():
    el = ET.fromstring('<Note><Tie destination="true"/></Note>')
    assert _note_is_tie(el) is True


def test_note_is_tie_origin_only_is_not_tie():
    el = ET.fromstring('<Note><Tie origin="true"/></Note>')
    assert _note_is_tie(el) is False


def test_note_is_tie_absent():
    assert _note_is_tie(ET.fromstring("<Note/>")) is False


# ── _gp6_element_variation_to_midi ──────────────────────────────────────────

def test_element_variation_out_of_range_is_none():
    assert _gp6_element_variation_to_midi(9999, 0) is None
    assert _gp6_element_variation_to_midi(-1, 0) is None


def test_element_variation_known_pieces():
    # Element 0 = kick (GM 35), element 1 = snare (GM 38). Pin exact values so a
    # mis-edit of the _GP6_EV / _ART_TO_MIDI tables is caught.
    assert _gp6_element_variation_to_midi(0, 0) == 35
    assert _gp6_element_variation_to_midi(1, 0) == 38


# ── _gpx_tuning ─────────────────────────────────────────────────────────────
# GPX string pitches are high->low (index 0 = highest string).

def test_tuning_6string_guitar_standard_is_zero():
    # E B G D A E (MIDI 64 59 55 50 45 40)
    assert _gpx_tuning({"string_pitches": [64, 59, 55, 50, 45, 40]}) == [0, 0, 0, 0, 0, 0]


def test_tuning_6string_guitar_eb_is_minus_one():
    assert _gpx_tuning({"string_pitches": [63, 58, 54, 49, 44, 39]}) == [-1, -1, -1, -1, -1, -1]


def test_tuning_4string_bass_standard_is_zero():
    # G D A E (high->low): 43 38 33 28
    assert _gpx_tuning({"string_pitches": [43, 38, 33, 28]}) == [0, 0, 0, 0]


def test_tuning_5string_low_b_standard_is_zero():
    # low-B 5-string, high->low: G D A E B = 43 38 33 28 23
    assert _gpx_tuning({"string_pitches": [43, 38, 33, 28, 23]}) == [0, 0, 0, 0, 0]


def test_tuning_5string_high_c_standard_is_zero():
    # high-C 5-string, high->low: C G D A E = 48 43 38 33 28.
    # Regression guard: previously forced the low-B reference and produced
    # non-zero offsets for a standard-tuned high-C bass.
    assert _gpx_tuning({"string_pitches": [48, 43, 38, 33, 28]}) == [0, 0, 0, 0, 0]


def test_tuning_empty_pitches_defaults_six_zero():
    assert _gpx_tuning({"string_pitches": []}) == [0, 0, 0, 0, 0, 0]
