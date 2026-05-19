"""Tests for lib/tones.py — tone extraction from an unpacked PSARC dir."""

import json

from tones import extract_arrangement_tones, sloppak_tone_changes


def _write_manifest(path, arr_name, tones=None, tone_ab=None):
    """Write a minimal RS-style manifest JSON at `path`."""
    attrs = {"ArrangementName": arr_name}
    if tones is not None:
        attrs["Tones"] = tones
    if tone_ab:
        attrs.update(tone_ab)
    path.write_text(json.dumps({"Entries": {"abc123": {"Attributes": attrs}}}))


def _write_xml(path, tonebase=None, changes=None):
    """Write a minimal arrangement `<song>` XML with tone elements."""
    parts = ["<song>"]
    if tonebase is not None:
        parts.append(f"<tonebase>{tonebase}</tonebase>")
    if changes is not None:
        parts.append("<tones>")
        for time, tid, name in changes:
            parts.append(f'<tone time="{time}" id="{tid}" name="{name}" />')
        parts.append("</tones>")
    parts.append("</song>")
    path.write_text("".join(parts))


# ── Full extraction ──────────────────────────────────────────────────────────

def test_extract_full_tone_block(tmp_path):
    tone_obj = {
        "Name": "Clean Rhythm",
        "Key": "Tone_A",
        "GearList": {"Amp": {"Type": "Amp_Twin", "KnobValues": {}}},
    }
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[tone_obj],
        tone_ab={"Tone_A": "Clean Rhythm", "Tone_B": "Lead Drive"},
    )
    _write_xml(
        tmp_path / "song_lead.xml",
        tonebase="Clean Rhythm",
        changes=[("12.5", "1", "Lead Drive"), ("4.0", "0", "Clean Rhythm")],
    )

    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result is not None
    assert result["base"] == "Clean Rhythm"
    # changes are time-sorted
    assert result["changes"] == [
        {"t": 4.0, "name": "Clean Rhythm"},
        {"t": 12.5, "name": "Lead Drive"},
    ]
    # definitions copied verbatim
    assert result["definitions"] == [tone_obj]


def test_na_change_name_resolved_via_id_map(tmp_path):
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": "Tone_A", "GearList": {}}],
        tone_ab={"Tone_A": "Clean", "Tone_B": "Distortion"},
    )
    _write_xml(
        tmp_path / "song_lead.xml",
        tonebase="Clean",
        changes=[("8.0", "1", "N/A")],
    )

    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result["changes"] == [{"t": 8.0, "name": "Distortion"}]


def test_base_falls_back_to_tone_a(tmp_path):
    """No <tonebase> in the XML → base derived from Tone_A."""
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": "Tone_A", "GearList": {}}],
        tone_ab={"Tone_A": "Clean"},
    )
    _write_xml(tmp_path / "song_lead.xml", changes=[("3.0", "0", "Clean")])

    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result["base"] == "Clean"


def test_fallback_xml_match_handles_spaced_arrangement_name(tmp_path):
    """No manifest entry → fall back to token XML match. A spaced
    arrangement name ("Bonus Lead") must still match an underscored
    stem ("song_bonus_lead")."""
    _write_xml(
        tmp_path / "song_bonus_lead.xml",
        tonebase="Clean",
        changes=[("5.0", "0", "Clean")],
    )
    result = extract_arrangement_tones(tmp_path, "Bonus Lead")
    assert result is not None
    assert result["base"] == "Clean"
    assert result["changes"] == [{"t": 5.0, "name": "Clean"}]


def test_fallback_xml_match_disambiguates_lead_from_bonus_lead(tmp_path):
    """Lead must bind song_lead, not song_bonus_lead — the token subset
    that adds the fewest extra tokens wins."""
    _write_xml(tmp_path / "song_lead.xml", tonebase="Plain Lead")
    _write_xml(tmp_path / "song_bonus_lead.xml", tonebase="Bonus Tone")
    assert extract_arrangement_tones(tmp_path, "Lead")["base"] == "Plain Lead"
    assert extract_arrangement_tones(tmp_path, "Bonus Lead")["base"] == "Bonus Tone"


def test_fallback_xml_match_ambiguous_tie_attaches_nothing(tmp_path):
    """When two XMLs match an arrangement equally well, attach neither —
    guessing risks pulling another arrangement's tone timeline."""
    _write_xml(tmp_path / "song_lead_a.xml", tonebase="A")
    _write_xml(tmp_path / "song_lead_b.xml", tonebase="B")
    assert extract_arrangement_tones(tmp_path, "Lead") is None


def test_non_hashable_tone_key_does_not_crash(tmp_path):
    """A non-string (unhashable) Key must not raise on the dedupe check."""
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": ["bad"], "GearList": {}}],
    )
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result is not None
    assert len(result["definitions"]) == 1


def test_malformed_manifest_string_fields_dont_crash(tmp_path):
    """Non-string ArrangementName / Tone_A must not raise or leak."""
    (tmp_path / "bad.json").write_text(json.dumps(
        {"Entries": {"x": {"Attributes": {"ArrangementName": 123, "Tones": []}}}}
    ))
    (tmp_path / "song_lead.json").write_text(json.dumps(
        {"Entries": {"y": {"Attributes": {
            "ArrangementName": "Lead",
            "Tone_A": {"not": "a string"},
            "Tones": [{"Name": "A", "Key": "Tone_A", "GearList": {}}],
        }}}}
    ))
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result is not None
    assert len(result["definitions"]) == 1
    # The non-string Tone_A must not leak in as the base tone name.
    assert "base" not in result


def test_non_dict_manifest_entry_is_skipped(tmp_path):
    """A non-dict entry or Attributes must not abort extraction."""
    (tmp_path / "bad.json").write_text(
        json.dumps({"Entries": {"x": ["not", "a", "dict"], "y": {"Attributes": []}}})
    )
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": "Tone_A", "GearList": {}}],
    )
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result is not None
    assert len(result["definitions"]) == 1


def test_non_finite_and_bad_change_times_are_skipped(tmp_path):
    """A malformed or non-finite `time` drops only that marker, not the
    whole arrangement."""
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": "Tone_A", "GearList": {}}],
    )
    _write_xml(
        tmp_path / "song_lead.xml",
        tonebase="A",
        changes=[
            ("nan", "0", "Bad"),
            ("xyz", "0", "AlsoBad"),
            ("7.0", "0", "Good"),
        ],
    )
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result["changes"] == [{"t": 7.0, "name": "Good"}]


def test_definitions_deduplicated_by_key(tmp_path):
    dup = {"Name": "Clean", "Key": "Tone_A", "GearList": {}}
    _write_manifest(tmp_path / "song_lead.json", "Lead", tones=[dup, dup])
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert len(result["definitions"]) == 1


# ── Negative cases ────────────────────────────────────────────────────────────

def test_vocals_arrangement_returns_none(tmp_path):
    assert extract_arrangement_tones(tmp_path, "Vocals") is None


def test_no_tone_data_returns_none(tmp_path):
    _write_manifest(tmp_path / "song_lead.json", "Lead")
    _write_xml(tmp_path / "song_lead.xml")
    assert extract_arrangement_tones(tmp_path, "Lead") is None


def test_unknown_arrangement_returns_none(tmp_path):
    _write_manifest(tmp_path / "song_lead.json", "Lead", tones=[{"Key": "Tone_A"}])
    assert extract_arrangement_tones(tmp_path, "Bass") is None


def test_unparseable_manifest_is_skipped(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": "Tone_A", "GearList": {}}],
    )
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result is not None
    assert len(result["definitions"]) == 1


def test_non_dict_top_level_json_is_skipped(tmp_path):
    """A *.json file whose top level is an array/scalar is not a manifest."""
    (tmp_path / "list.json").write_text(json.dumps([1, 2, 3]))
    _write_manifest(
        tmp_path / "song_lead.json",
        "Lead",
        tones=[{"Name": "A", "Key": "Tone_A", "GearList": {}}],
    )
    result = extract_arrangement_tones(tmp_path, "Lead")
    assert result is not None
    assert len(result["definitions"]) == 1


# ── sloppak_tone_changes (highway payload builder) ───────────────────────────

def test_sloppak_tone_changes_sorts_and_returns_base():
    base, changes = sloppak_tone_changes({
        "base": "Clean",
        "changes": [{"t": 12.5, "name": "Drive"}, {"t": 3.0, "name": "Clean"}],
    })
    assert base == "Clean"
    assert changes == [{"t": 3.0, "name": "Clean"}, {"t": 12.5, "name": "Drive"}]


def test_sloppak_tone_changes_skips_malformed_markers():
    _, changes = sloppak_tone_changes({
        "changes": [
            {"t": "nan", "name": "BadStr"},
            {"t": float("inf"), "name": "Inf"},
            {"t": 5.0, "name": 123},          # non-string name
            {"t": None, "name": "NoTime"},
            "not-a-dict",
            {"t": 7.0, "name": "Good"},
        ],
    })
    assert changes == [{"t": 7.0, "name": "Good"}]


def test_sloppak_tone_changes_handles_none_and_bad_base():
    assert sloppak_tone_changes(None) == ("", [])
    base, changes = sloppak_tone_changes({"base": 123, "changes": []})
    assert base == "" and changes == []


def test_sloppak_tone_changes_non_dict_input():
    """A truthy non-dict payload must not crash."""
    assert sloppak_tone_changes(["not", "a", "dict"]) == ("", [])
    assert sloppak_tone_changes("nope") == ("", [])


def test_sloppak_tone_changes_non_list_changes():
    """A truthy non-list `changes` value must not raise on iteration."""
    base, changes = sloppak_tone_changes({"base": "Clean", "changes": 1})
    assert base == "Clean" and changes == []


def test_non_list_tones_field_is_skipped(tmp_path):
    """A non-list `Tones` value must not raise during iteration."""
    (tmp_path / "song_lead.json").write_text(json.dumps(
        {"Entries": {"y": {"Attributes": {
            "ArrangementName": "Lead",
            "Tone_A": "Clean",
            "Tones": {"not": "a list"},
        }}}}
    ))
    _write_xml(tmp_path / "song_lead.xml", tonebase="Clean")
    result = extract_arrangement_tones(tmp_path, "Lead")
    # No definitions (Tones was malformed) but base still resolves.
    assert result is not None
    assert result.get("definitions") is None or result["definitions"] == []
    assert result["base"] == "Clean"
