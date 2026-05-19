"""End-to-end tests for convert_psarc_to_sloppak().

Uses the real SS_PonyIcon_p.psarc fixture to exercise the pure-Python
parts of the pipeline (psarc unpack, song parsing, lyrics/cover
extraction, manifest serialization, arrangement JSON wire format,
zip layout). Two non-Python boundaries are stubbed so CI doesn't need
native binaries: song._convert_sng_to_xml (RsCli, .NET) and
sloppak_convert._wem_to_ogg (vgmstream-cli + ffmpeg).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

import song
import sloppak_convert
from sloppak_convert import convert_psarc_to_sloppak


FIXTURE = Path(__file__).parent / "fixtures" / "SS_PonyIcon_p.psarc"
XML_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "SS_PonyIcon_xml"


def _stub_wem_to_ogg(wem_path, out_ogg: Path) -> None:
    """Skip vgmstream/ffmpeg; write a sentinel OGG so the zip step has a file
    to pack and the size guard inside the real helper is bypassed."""
    out_ogg.parent.mkdir(parents=True, exist_ok=True)
    out_ogg.write_bytes(b"OggS" + b"\x00" * 256)


def _stub_sng_to_xml(extracted_dir: str) -> None:
    """Replace song._convert_sng_to_xml — the SNG→XML hop normally needs RsCli
    (a .NET binary not on PATH in CI). Copy pre-converted pony XMLs into the
    standard songs/arr/ location load_song scans."""
    arr_dir = Path(extracted_dir) / "songs" / "arr"
    arr_dir.mkdir(parents=True, exist_ok=True)
    for xml in XML_FIXTURE_DIR.glob("*.xml"):
        shutil.copy(xml, arr_dir / xml.name)


def _apply_pony_stubs(mp) -> None:
    """Apply the two stubs that let convert_psarc_to_sloppak run on pony psarc
    in CI: SNG→XML conversion and WEM→OGG transcoding."""
    mp.setattr(song, "_convert_sng_to_xml", _stub_sng_to_xml)
    mp.setattr(sloppak_convert, "_wem_to_ogg", _stub_wem_to_ogg)


# ── Shared session-scoped conversion ─────────────────────────────────────────
# The pony PSARC takes a few seconds to unpack + parse; running it once per
# session keeps the suite cheap. Cases 1-7 read this fixture; cases 8-11
# stand alone with their own monkeypatches.

def _require_fixture() -> None:
    """The pony psarc and pre-converted XMLs are checked in — a missing file
    is a setup bug, not a skip condition. Fail loudly so CI catches a broken
    checkout instead of letting it surface later as a conversion error."""
    if not FIXTURE.is_file():
        pytest.fail(f"required test fixture missing: {FIXTURE}")
    if not list(XML_FIXTURE_DIR.glob("*.xml")):
        pytest.fail(f"required XML fixtures missing under: {XML_FIXTURE_DIR}")


@pytest.fixture(scope="session")
def converted_pony(tmp_path_factory) -> Path:
    _require_fixture()
    out_zip = tmp_path_factory.mktemp("pony") / "pony.sloppak"
    # session-scoped MonkeyPatch (pytest's `monkeypatch` is function-scoped).
    mp = pytest.MonkeyPatch()
    try:
        _apply_pony_stubs(mp)
        convert_psarc_to_sloppak(FIXTURE, out_zip)
    finally:
        mp.undo()
    return out_zip


@pytest.fixture(scope="session")
def pony_zip(converted_pony: Path):
    with zipfile.ZipFile(converted_pony, "r") as zf:
        yield zf


@pytest.fixture(scope="session")
def pony_manifest(pony_zip: zipfile.ZipFile) -> dict:
    return yaml.safe_load(pony_zip.read("manifest.yaml"))


# ── Case 1: zip output exists and opens ──────────────────────────────────────

def test_output_is_a_valid_zip(converted_pony: Path):
    assert converted_pony.exists()
    assert converted_pony.stat().st_size > 0
    assert zipfile.is_zipfile(converted_pony)


# ── Case 2: manifest shape ───────────────────────────────────────────────────

def test_manifest_has_required_keys(pony_manifest: dict):
    for key in ("title", "artist", "album", "year", "duration", "stems", "arrangements"):
        assert key in pony_manifest, f"manifest missing {key!r}"


def test_manifest_metadata_types_and_values(pony_manifest: dict):
    assert isinstance(pony_manifest["title"], str) and pony_manifest["title"]
    assert isinstance(pony_manifest["artist"], str) and pony_manifest["artist"]
    assert isinstance(pony_manifest["year"], int)
    assert isinstance(pony_manifest["duration"], float)
    assert pony_manifest["duration"] > 0


def test_manifest_matches_known_pony_metadata(pony_manifest: dict):
    """Pin the metadata so a regression in song.load_song surfaces immediately."""
    assert pony_manifest["title"] == "Pony Icon"
    assert pony_manifest["artist"] == "SnowShovel"
    assert pony_manifest["year"] == 2024


# ── Case 3: arrangements present, parseable, ids unique ──────────────────────

def test_every_manifest_arrangement_has_a_zip_member(
    pony_manifest: dict, pony_zip: zipfile.ZipFile
):
    names = set(pony_zip.namelist())
    for arr in pony_manifest["arrangements"]:
        assert arr["file"] in names, f"missing arrangement file {arr['file']}"


def test_arrangement_jsons_are_valid_json(
    pony_manifest: dict, pony_zip: zipfile.ZipFile
):
    for arr in pony_manifest["arrangements"]:
        data = json.loads(pony_zip.read(arr["file"]))
        assert isinstance(data, dict)
        assert "notes" in data and "name" in data


def test_arrangement_ids_are_unique(pony_manifest: dict):
    ids = [a["id"] for a in pony_manifest["arrangements"]]
    assert len(ids) == len(set(ids))


# ── Case 4: only the first arrangement carries beats/sections ────────────────

def test_only_first_arrangement_has_beats_and_sections(
    pony_manifest: dict, pony_zip: zipfile.ZipFile
):
    arrs = pony_manifest["arrangements"]
    assert len(arrs) >= 2, "fixture must have ≥2 arrangements to exercise this invariant"

    first = json.loads(pony_zip.read(arrs[0]["file"]))
    assert "beats" in first and len(first["beats"]) > 0
    assert "sections" in first and len(first["sections"]) > 0

    for arr in arrs[1:]:
        rest = json.loads(pony_zip.read(arr["file"]))
        assert "beats" not in rest, f"{arr['id']} should not carry beats"
        assert "sections" not in rest, f"{arr['id']} should not carry sections"


# ── Case 5: stems manifest references a real zip member ──────────────────────

def test_stems_manifest_points_at_full_ogg(
    pony_manifest: dict, pony_zip: zipfile.ZipFile
):
    stems = pony_manifest["stems"]
    assert stems == [{"id": "full", "file": "stems/full.ogg", "default": "on"}]
    assert "stems/full.ogg" in pony_zip.namelist()


# ── Case 6: pony psarc has lyrics ────────────────────────────────────────────

def test_lyrics_extracted(pony_manifest: dict, pony_zip: zipfile.ZipFile):
    assert pony_manifest.get("lyrics") == "lyrics.json"
    assert "lyrics.json" in pony_zip.namelist()
    lyrics = json.loads(pony_zip.read("lyrics.json"))
    assert isinstance(lyrics, list) and len(lyrics) > 0
    assert {"t", "d", "w"} <= set(lyrics[0].keys())


# ── Case 7: pony psarc has cover art ─────────────────────────────────────────

def test_cover_extracted(pony_manifest: dict, pony_zip: zipfile.ZipFile):
    assert pony_manifest.get("cover") == "cover.jpg"
    assert "cover.jpg" in pony_zip.namelist()
    # JPEG SOI marker — proves PIL produced a real JPEG, not some stub.
    assert pony_zip.read("cover.jpg")[:2] == b"\xff\xd8"


# ── Case 8: as_dir=True produces directory layout ────────────────────────────

def test_as_dir_produces_matching_layout(tmp_path: Path, monkeypatch):
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out_dir = tmp_path / "pony.sloppak"
    convert_psarc_to_sloppak(FIXTURE, out_dir, as_dir=True)

    assert out_dir.is_dir()
    manifest = yaml.safe_load((out_dir / "manifest.yaml").read_text())
    for arr in manifest["arrangements"]:
        assert (out_dir / arr["file"]).is_file()
    for stem in manifest["stems"]:
        assert (out_dir / stem["file"]).is_file()


# ── Case 9: no-arrangements → RuntimeError ───────────────────────────────────

def test_raises_when_no_arrangements(tmp_path: Path, monkeypatch):
    _require_fixture()

    real_load_song = sloppak_convert.load_song

    def empty_arrangements(extracted_dir: str):
        # Don't shadow the imported `song` module — pick a distinct local name.
        loaded_song = real_load_song(extracted_dir)
        loaded_song.arrangements = []
        return loaded_song

    _apply_pony_stubs(monkeypatch)
    monkeypatch.setattr(sloppak_convert, "load_song", empty_arrangements)

    with pytest.raises(RuntimeError, match="no playable arrangements"):
        convert_psarc_to_sloppak(FIXTURE, tmp_path / "out.sloppak")


# ── Case 10: no-WEM → RuntimeError ───────────────────────────────────────────

def test_raises_when_no_wem(tmp_path: Path, monkeypatch):
    _require_fixture()
    _apply_pony_stubs(monkeypatch)
    monkeypatch.setattr(sloppak_convert, "find_wem_files", lambda _d: [])

    with pytest.raises(RuntimeError, match="no WEM audio"):
        convert_psarc_to_sloppak(FIXTURE, tmp_path / "out.sloppak")


# ── Case 11: progress callback receives terminal frame ───────────────────────

def test_progress_callback_terminates_at_done(tmp_path: Path, monkeypatch):
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    frames: list[tuple[float, str, str]] = []
    convert_psarc_to_sloppak(
        FIXTURE,
        tmp_path / "out.sloppak",
        progress_cb=lambda frac, stage, msg: frames.append((frac, stage, msg)),
    )

    assert frames, "expected at least one progress callback"
    last_frac, last_stage, _ = frames[-1]
    assert last_frac == 1.0
    assert last_stage == "done"
    # Stages should advance monotonically through the documented phases.
    stages = [s for _, s, _ in frames]
    assert "extracting" in stages
    assert "packing" in stages
    assert stages.index("extracting") < stages.index("packing") < stages.index("done")


# ── Atomic-write contract (issue topkoa/slopsmith-plugin-sloppak-converter#25) ─


def test_zip_write_is_atomic_no_tmp_sibling_after_success(tmp_path: Path, monkeypatch):
    """Happy path: a finished convert leaves only `out.sloppak`, no
    half-written `.tmp` sibling. Guards against accidentally bypassing
    the rename step in future refactors."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)
    out = tmp_path / "out.sloppak"
    convert_psarc_to_sloppak(FIXTURE, out)
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["out.sloppak"], (
        f"expected only the final sloppak in the output dir, got {siblings}"
    )


def test_zip_write_failure_does_not_clobber_existing_out_path(tmp_path: Path, monkeypatch):
    """A pre-existing sloppak at `out_path` must be preserved verbatim
    when a conversion fails mid-write. Without the atomic `.tmp` +
    rename pattern, the previous direct-write path could leave a
    partial / truncated zip on top of the user's existing file."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "existing.sloppak"
    sentinel = b"PRE-EXISTING-CONTENT" + b"\x00" * 128
    out.write_bytes(sentinel)

    # Force the zip step to blow up after extraction completes.
    def _explode(*args, **kwargs):
        raise RuntimeError("simulated mid-write crash")
    monkeypatch.setattr(sloppak_convert, "_zip_dir", _explode)

    with pytest.raises(RuntimeError, match="simulated mid-write crash"):
        convert_psarc_to_sloppak(FIXTURE, out)

    # Existing file untouched.
    assert out.read_bytes() == sentinel
    # No leftover `.tmp` staging file.
    assert not (tmp_path / "existing.sloppak.tmp").exists()


def test_dir_write_failure_does_not_clobber_existing_out_dir(tmp_path: Path, monkeypatch):
    """Same contract for the `as_dir=True` path: pre-existing dir-form
    sloppak survives a failed convert. The two-rename swap pattern
    bounds the window to a single rename rather than the rmtree +
    copytree the previous code did."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "existing.sloppak"
    out.mkdir()
    (out / "sentinel.txt").write_text("untouched", encoding="utf-8")

    # `as_dir=True` uses `shutil.copytree` to stage at `.tmp`. Make
    # the copytree raise to simulate a kill mid-stage. Patching the
    # symbol on the module under test (sloppak_convert.shutil) so the
    # production code path sees the explosion.
    def _explode(*args, **kwargs):
        raise RuntimeError("simulated mid-write crash (dir form)")
    monkeypatch.setattr(sloppak_convert.shutil, "copytree", _explode)

    with pytest.raises(RuntimeError, match="simulated mid-write crash"):
        convert_psarc_to_sloppak(FIXTURE, out, as_dir=True)

    # Existing dir preserved with its sentinel content.
    assert (out / "sentinel.txt").read_text(encoding="utf-8") == "untouched"
    # No leftover `.tmp` staging dir, no `.old` backup left lying around.
    assert not (tmp_path / "existing.sloppak.tmp").exists()
    assert not (tmp_path / "existing.sloppak.old").exists()


def test_zip_convert_cleans_stale_dir_form_tmp(tmp_path: Path, monkeypatch):
    """A leftover `.tmp` directory from a previously-killed `as_dir=True`
    convert must not block a fresh `as_dir=False` convert from staging
    its zip at the same `.tmp` path."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "out.sloppak"
    stale = tmp_path / "out.sloppak.tmp"
    stale.mkdir()
    (stale / "from-prior-dir-convert.txt").write_text("ghost")

    convert_psarc_to_sloppak(FIXTURE, out)

    assert out.exists() and out.is_file()
    assert not stale.exists()


def test_dir_convert_cleans_stale_zip_form_tmp(tmp_path: Path, monkeypatch):
    """Mirror case: a leftover `.tmp` *file* from a previously-killed
    `as_dir=False` convert must not block a fresh `as_dir=True` convert."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "out.sloppak"
    stale = tmp_path / "out.sloppak.tmp"
    stale.write_bytes(b"ghost zip from prior crash")

    convert_psarc_to_sloppak(FIXTURE, out, as_dir=True)

    assert out.exists() and out.is_dir()
    assert not stale.exists()


def test_dir_convert_handles_pre_existing_file_at_out_path(tmp_path: Path, monkeypatch):
    """User reconverts `as_dir=True` over an existing zip-form sloppak —
    the file at `out_path` must be replaced atomically without leaving
    a `.old` file sidecar."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "out.sloppak"
    out.write_bytes(b"prior zip-form sloppak")

    convert_psarc_to_sloppak(FIXTURE, out, as_dir=True)

    assert out.exists() and out.is_dir()
    assert not (tmp_path / "out.sloppak.tmp").exists()
    assert not (tmp_path / "out.sloppak.old").exists()


def test_dir_convert_cleans_stale_file_backup(tmp_path: Path, monkeypatch):
    """A pre-existing `<out>.sloppak.old` *file* (e.g. left behind from
    a kill during a prior `as_dir=True` swap when out_path was a zip-
    form sloppak) must not crash the next convert with
    `NotADirectoryError`."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "out.sloppak"
    stale_backup = tmp_path / "out.sloppak.old"
    stale_backup.write_bytes(b"ghost zip from interrupted swap")

    convert_psarc_to_sloppak(FIXTURE, out, as_dir=True)

    assert out.exists() and out.is_dir()
    assert not stale_backup.exists()


def test_zip_convert_handles_pre_existing_dir_at_out_path(tmp_path: Path, monkeypatch):
    """User reconverts `as_dir=False` (zip form) over an existing dir-
    form sloppak — `os.replace` can't swap file-onto-dir, so the dir
    must be cleared first. Mirrors the as_dir=True branch's handling
    of pre-existing file at out_path."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "out.sloppak"
    out.mkdir()
    (out / "from-prior-dir-convert.txt").write_text("ghost", encoding="utf-8")

    convert_psarc_to_sloppak(FIXTURE, out)

    assert out.exists() and out.is_file()
    # No leftover staging file.
    assert not (tmp_path / "out.sloppak.tmp").exists()


def test_finally_cleans_old_sidecar_on_failed_swap_rename(tmp_path: Path, monkeypatch):
    """Simulate a crash *after* `out_path.rename(backup)` succeeded but
    *before* `tmp_out.rename(out_path)` did. The rollback path renames
    `backup` back, but the `finally` is what guarantees no stale `.old`
    survives if rollback itself stumbles."""
    _require_fixture()
    _apply_pony_stubs(monkeypatch)

    out = tmp_path / "out.sloppak"
    out.mkdir()
    (out / "sentinel.txt").write_text("from-original", encoding="utf-8")

    # Make the *second* Path.rename call fail (the first is
    # `out_path.rename(backup)`; the second is `tmp_out.rename(out_path)`).
    original_rename = Path.rename
    call_count = [0]

    def _flaky_rename(self, target):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("simulated rename failure mid-swap")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    with pytest.raises(RuntimeError, match="simulated rename failure mid-swap"):
        convert_psarc_to_sloppak(FIXTURE, out, as_dir=True)

    # Rollback restored the original dir.
    assert out.exists() and out.is_dir()
    assert (out / "sentinel.txt").read_text(encoding="utf-8") == "from-original"
    # `.old` swept up by the finally.
    assert not (tmp_path / "out.sloppak.old").exists()
    # `.tmp` swept up by the finally.
    assert not (tmp_path / "out.sloppak.tmp").exists()
