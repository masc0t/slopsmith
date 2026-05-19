"""Tone extraction from an unpacked Rocksmith PSARC.

A Rocksmith arrangement carries two distinct kinds of tone data:

* **Definitions** — the full amp / pedal / cabinet signal chains, stored in
  the manifest JSON under ``Entries[*].Attributes.Tones`` (a list of raw RS
  tone objects: ``{"Name", "Key", "GearList", ...}``).
* **Changes** — the in-song tone switches (``<tones><tone time= id= name=/>``)
  plus the initial ``<tonebase>`` tone, stored in the arrangement XML.

The PSARC scanner keeps archives read-only and never persists this, so the
sloppak converter calls :func:`extract_arrangement_tones` to lift it out of an
already-unpacked PSARC directory and embed it inline in the arrangement JSON
(see ``lib/song.py`` ``arrangement_to_wire`` / the ``tones`` wire key).

Definitions are copied **verbatim** — parsing them into rendered gear chains
needs the gear-name/image map that lives in the Tones plugin, so the plugin
keeps owning that step. This module stays gear-map-free and pure.
"""

from __future__ import annotations

import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("slopsmith.lib.tones")

# Arrangement names that never carry guitar tones.
_NON_TONE_ARRANGEMENTS = {"vocals", "showlights", "jvocals"}


def tokens(s: str) -> set[str]:
    """Split a name or file stem into lowercased alphanumeric tokens.

    Used for fuzzy arrangement↔XML matching: arrangement names carry spaces
    ("Bonus Lead") while file stems are underscored ("song_bonus_lead"), and a
    plain substring check is ambiguous ("lead" is a substring of "bonuslead").
    Shared with the PSARC playback path in `server.py` so the two stay
    consistent.
    """
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t}


def _manifest_tone_data(
    json_files: list[Path], arr_name: str
) -> tuple[list[dict], dict[int, str], str | None]:
    """Find the manifest JSON entry for ``arr_name``.

    ``json_files`` is a pre-scanned list of the unpacked PSARC's ``*.json``
    files — passed in (rather than re-globbed here) so a multi-arrangement
    conversion scans the tree only once.

    Returns ``(definitions, id_name_map, json_stem)`` where ``definitions`` is
    the de-duplicated raw ``Tones`` list, ``id_name_map`` maps tone index
    (0-3) → display name from the ``Tone_A``..``Tone_D`` attributes, and
    ``json_stem`` is the lowercased manifest filename stem (used to locate the
    matching arrangement XML, since RS names the JSON and XML identically).
    """
    target = (arr_name or "").strip().lower()
    for jf in json_files:
        try:
            # Decode strictly — `errors="ignore"` could drop bad bytes and
            # yield a parseable-but-corrupted manifest, silently mangling
            # arrangement / tone names. A real decode failure is caught here.
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            log.debug("tones: skipping unparseable manifest %s: %s", jf.name, e)
            continue
        if not isinstance(data, dict):
            # `_scan()` feeds every *.json file in — a valid JSON array /
            # string / number is not a manifest; skip it.
            continue
        entries = data.get("Entries")
        if not isinstance(entries, dict):
            # A malformed manifest may carry `Entries` as a list (or omit it);
            # skip rather than letting `.values()` raise AttributeError.
            continue
        for entry in entries.values():
            # A corrupt/hand-edited manifest may carry non-dict entries or a
            # non-dict `Attributes`; skip them rather than letting `.get()`
            # raise and abort tone extraction for the whole song.
            if not isinstance(entry, dict):
                continue
            attrs = entry.get("Attributes")
            if not isinstance(attrs, dict):
                continue
            raw_name = attrs.get("ArrangementName")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name or name.lower() != target:
                continue

            definitions: list[dict] = []
            seen: set[str] = set()
            raw_tones = attrs.get("Tones")
            if not isinstance(raw_tones, list):
                # A malformed manifest could set `Tones` to a non-list;
                # iterating it would misbehave or raise.
                raw_tones = []
            for tone in raw_tones:
                if not isinstance(tone, dict):
                    continue
                # Only dedupe on a string Key — a non-string (list/dict)
                # would raise TypeError on the `in seen` membership test.
                key = tone.get("Key", "")
                if isinstance(key, str) and key:
                    if key in seen:
                        continue
                    seen.add(key)
                definitions.append(tone)

            id_name_map: dict[int, str] = {}
            for idx, key in enumerate(("Tone_A", "Tone_B", "Tone_C", "Tone_D")):
                val = attrs.get(key)
                # Only accept string tone names — a malformed manifest could
                # carry a number/object, which must not leak into the
                # serialized tone block.
                if isinstance(val, str) and val:
                    id_name_map[idx] = val

            return definitions, id_name_map, jf.stem.lower()
    return [], {}, None


def _xml_tone_changes(
    xml_path: Path, id_name_map: dict[int, str]
) -> tuple[str, list[dict]]:
    """Parse ``<tonebase>`` and ``<tones>`` out of an arrangement XML.

    ``"N/A"`` / empty change names are resolved through ``id_name_map`` (the
    same fallback the highway WebSocket applies for PSARC playback).
    """
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError) as e:
        log.debug("tones: failed to parse arrangement XML %s: %s", xml_path.name, e)
        return "", []
    if root.tag != "song":
        return "", []

    base = ""
    tonebase = root.find("tonebase")
    if tonebase is not None and tonebase.text:
        base = tonebase.text.strip()

    changes: list[dict] = []
    tones_el = root.find("tones")
    if tones_el is not None:
        for t in tones_el.findall("tone"):
            tc_time = t.get("time")
            tc_name = t.get("name", "")
            tc_id = t.get("id", "")
            if (not tc_name or tc_name == "N/A") and tc_id:
                try:
                    tc_name = id_name_map.get(int(tc_id), f"Tone {tc_id}")
                except ValueError:
                    pass
            if tc_time and tc_name:
                try:
                    t_val = float(tc_time)
                except (TypeError, ValueError):
                    continue
                # Drop non-finite markers — NaN/inf would serialize into the
                # sloppak JSON as unparseable tokens for the client.
                if not math.isfinite(t_val):
                    continue
                changes.append({"t": round(t_val, 3), "name": tc_name})
    return base, changes


def _extract_one(
    arr_name: str, json_files: list[Path], xml_files: list[Path]
) -> dict | None:
    """Extract the tone block for one arrangement from pre-scanned file lists."""
    if (arr_name or "").strip().lower() in _NON_TONE_ARRANGEMENTS:
        return None

    definitions, id_name_map, json_stem = _manifest_tone_data(json_files, arr_name)

    # Locate the matching arrangement XML. RS names the manifest JSON and the
    # arrangement XML with the same stem; fall back to a fuzzy name match for
    # CDLC that doesn't ship a manifest entry.
    xml_path: Path | None = None
    if json_stem:
        for xf in xml_files:
            if xf.stem.lower() == json_stem:
                xml_path = xf
                break
    if xml_path is None:
        # No manifest entry (CDLC that ships none) — match the arrangement
        # XML by name tokens. Require the arrangement's tokens to be a subset
        # of the stem's, and pick the stem with the fewest *extra* tokens, so
        # "Lead" binds `song_lead` rather than `song_bonus_lead` while
        # "Bonus Lead" still binds the latter.
        target_tokens = tokens(arr_name)
        if target_tokens:
            candidates: list[tuple[int, Path]] = []
            for xf in xml_files:
                stem_tokens = tokens(xf.stem)
                if target_tokens <= stem_tokens:
                    candidates.append((len(stem_tokens - target_tokens), xf))
            if candidates:
                best_extra = min(extra for extra, _ in candidates)
                tied = [xf for extra, xf in candidates if extra == best_extra]
                if len(tied) == 1:
                    xml_path = tied[0]
                else:
                    # Ambiguous — several XMLs match equally well. Picking
                    # one arbitrarily risks attaching another arrangement's
                    # tone timeline, so attach none.
                    log.debug(
                        "tones: ambiguous fallback XML for %r: %s",
                        arr_name, [x.name for x in tied],
                    )

    base, changes = ("", [])
    if xml_path is not None:
        base, changes = _xml_tone_changes(xml_path, id_name_map)

    # Fall back to Tone_A as the base when the XML didn't name one.
    if not base and 0 in id_name_map:
        base = id_name_map[0]

    if not base and not changes and not definitions:
        return None

    result: dict = {}
    if base:
        result["base"] = base
    if changes:
        result["changes"] = sorted(changes, key=lambda c: c["t"])
    if definitions:
        result["definitions"] = definitions
    return result


def _scan(extracted_dir: Path) -> tuple[list[Path], list[Path]]:
    """Scan an unpacked PSARC once for its manifest JSON and arrangement XML."""
    return (
        sorted(extracted_dir.rglob("*.json")),
        sorted(extracted_dir.rglob("*.xml")),
    )


def extract_arrangement_tones(extracted_dir, arr_name: str) -> dict | None:
    """Extract the tone block for one arrangement from an unpacked PSARC.

    ``extracted_dir`` is a directory produced by ``unpack_psarc``; ``arr_name``
    is the resolved arrangement name (e.g. ``"Lead"``) as produced by
    ``song.load_song``.

    Returns a dict with any of ``base`` (str), ``changes``
    (``[{"t", "name"}]``, time-sorted) and ``definitions`` (raw RS tone
    objects), or ``None`` when the arrangement has no tone data at all.

    For a multi-arrangement conversion prefer :func:`extract_tones_for_song`,
    which scans the directory once instead of per call.
    """
    json_files, xml_files = _scan(Path(extracted_dir))
    return _extract_one(arr_name, json_files, xml_files)


def sloppak_tone_changes(arr_tones) -> tuple[str, list[dict]]:
    """Build the highway tone-change payload from an arrangement's tone block.

    Given ``Arrangement.tones`` (the dict the converter embeds, or ``None``),
    returns ``(base, changes)`` where ``base`` is the initial tone name and
    ``changes`` is a time-sorted ``[{"t", "name"}]`` list. Non-string names,
    non-dict entries, and non-numeric / non-finite times are skipped — a
    hand-edited or third-party sloppak must not crash the highway WebSocket
    or emit NaN/inf (which the client's ``JSON.parse`` rejects).
    """
    if not isinstance(arr_tones, dict):
        return "", []
    base_val = arr_tones.get("base", "")
    base = base_val.strip() if isinstance(base_val, str) else ""

    changes: list[dict] = []
    raw_changes = arr_tones.get("changes")
    if not isinstance(raw_changes, list):
        # A truthy non-list (e.g. `1`) would raise TypeError on iteration.
        raw_changes = []
    for c in raw_changes:
        if not isinstance(c, dict):
            continue
        t = c.get("t")
        name = c.get("name")
        if t is None or not isinstance(name, str) or not name:
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(t):
            continue
        changes.append({"t": round(t, 3), "name": name})
    changes.sort(key=lambda x: x["t"])
    return base, changes


def extract_tones_for_song(extracted_dir, arr_names) -> dict[str, dict]:
    """Extract tone blocks for several arrangements, scanning the tree once.

    Returns ``{arr_name: tone_block}`` containing only the arrangements that
    actually have tone data — arrangements with none are omitted.
    """
    json_files, xml_files = _scan(Path(extracted_dir))
    out: dict[str, dict] = {}
    for name in arr_names:
        block = _extract_one(name, json_files, xml_files)
        if block:
            out[name] = block
    return out
