"""Path-traversal rejection for archive extractors.

`lib/psarc.py::unpack_psarc`, `lib/patcher.py::unpack_psarc`, and
`lib/sloppak.py::_unpack_zip` all consume attacker-controlled archive entry
names from files that land in `DLC_DIR`. A crafted entry name with `..`
segments, an absolute path, or backslash separators must not write outside
the destination directory.
"""

from __future__ import annotations

import logging
import struct
import zipfile
import zlib
from pathlib import Path

import pytest


def _build_evil_psarc(out_path: Path, evil_name: str, payload: bytes = b"pwned") -> None:
    """Write an uncompressed-TOC PSARC whose single payload entry has a
    traversal filename.
    """
    file_list = (evil_name + "\n").encode("utf-8")

    c0 = zlib.compress(file_list)
    c1 = zlib.compress(payload)
    block_table = struct.pack(">HH", len(c0), len(c1))

    def toc_entry(z_index: int, length: int, offset: int) -> bytes:
        return (
            b"\x00" * 16
            + struct.pack(">I", z_index)
            + length.to_bytes(5, "big")
            + offset.to_bytes(5, "big")
        )

    HEADER_SIZE = 32
    TOC_ENTRY_SIZE = 30
    NUM_ENTRIES = 2
    toc_data_size = TOC_ENTRY_SIZE * NUM_ENTRIES
    toc_region_size = toc_data_size + len(block_table)
    toc_length = HEADER_SIZE + toc_region_size

    off0 = HEADER_SIZE + toc_region_size
    off1 = off0 + len(c0)

    toc = (
        toc_entry(0, len(file_list), off0)
        + toc_entry(1, len(payload), off1)
    )

    header = (
        b"PSAR"
        + struct.pack(">I", 0x00010004)
        + b"zlib"
        + struct.pack(">I", toc_length)
        + struct.pack(">I", TOC_ENTRY_SIZE)
        + struct.pack(">I", NUM_ENTRIES)
        + struct.pack(">I", 65536)
        + struct.pack(">I", 0)  # archive_flags=0 → unencrypted TOC
    )

    out_path.write_bytes(header + toc + block_table + c0 + c1)


# ── PSARC ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "evil_name_template",
    [
        "../escaped.txt",
        "../" * 6 + "tmp/pwn.txt",
        "{abs}",                            # absolute path outside extract dir
        "subdir/../../escaped.txt",
        "..\\windows.txt",
        "\x00null_byte.txt",  # crash-on-resolve must reject cleanly, not raise
        ".",                                # resolves to extract dir itself
        "subdir/..",                        # contained-but-degenerate → root
    ],
)
def test_unpack_psarc_rejects_traversal(tmp_path, evil_name_template, caplog):
    from psarc import unpack_psarc

    psarc = tmp_path / "evil.psarc"
    extract = tmp_path / "extract"
    extract.mkdir()
    canary = tmp_path / "canary"  # sibling of extract — should never be written
    abs_target = (tmp_path / "abs.txt").resolve()
    evil_name = evil_name_template.format(abs=str(abs_target))

    _build_evil_psarc(psarc, evil_name)

    with caplog.at_level(logging.WARNING, logger="slopsmith.lib.psarc"):
        extracted = unpack_psarc(str(psarc), str(extract))
    # Names that resolve to the output root itself ("." / "subdir/..") must
    # hit the specific root-rejection branch, not the generic "unsafe entry
    # path" or the silent IsADirectoryError fallback.
    if evil_name in (".", "subdir/.."):
        assert any(
            "resolving to output root" in r.getMessage() for r in caplog.records
        ), caplog.records

    # Nothing escaped the extract dir.
    for p in extract.rglob("*"):
        if p.is_file():
            assert extract.resolve() in p.resolve().parents
    assert not canary.exists()
    # Nor did the resolved-traversal target land on disk.
    assert not (tmp_path / "escaped.txt").exists()
    assert not abs_target.exists()
    # The function returned no extracted files (the only entry was unsafe).
    assert extracted == []


def test_unpack_psarc_allows_safe_entry(tmp_path):
    """Sanity: a benign filename still extracts normally."""
    from psarc import unpack_psarc

    psarc = tmp_path / "good.psarc"
    extract = tmp_path / "extract"
    extract.mkdir()

    _build_evil_psarc(psarc, "subdir/hello.txt", payload=b"hello")
    extracted = unpack_psarc(str(psarc), str(extract))

    assert (extract / "subdir" / "hello.txt").read_bytes() == b"hello"
    assert len(extracted) == 1


# ── patcher (duplicate impl) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "evil_name",
    [
        "../" * 6 + "tmp/patcher_pwn.txt",
        ".",
        "subdir/..",
    ],
)
def test_patcher_unpack_psarc_rejects_traversal(tmp_path, evil_name, caplog):
    from patcher import unpack_psarc as patcher_unpack

    psarc = tmp_path / "evil.psarc"
    extract = tmp_path / "extract"
    extract.mkdir()

    _build_evil_psarc(psarc, evil_name)
    with caplog.at_level(logging.WARNING, logger="slopsmith.lib.patcher"):
        patcher_unpack(str(psarc), str(extract))

    for p in extract.rglob("*"):
        if p.is_file():
            assert extract.resolve() in p.resolve().parents
    assert not (tmp_path / "patcher_pwn.txt").exists()
    if evil_name in (".", "subdir/.."):
        assert any(
            "resolving to output root" in r.getMessage() for r in caplog.records
        ), caplog.records


# ── Sloppak zip-slip ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "evil_name_template",
    [
        "../escaped.txt",
        "../" * 8 + "tmp/SLOPSMITH_PWNED_SLOPPAK",
        "{abs}",
        "subdir/../../escaped.txt",
        ".",
        "subdir/..",
    ],
)
def test_unpack_sloppak_zip_rejects_traversal(tmp_path, evil_name_template, caplog):
    from sloppak import _unpack_zip

    src = tmp_path / "evil.sloppak"
    dest = tmp_path / "dest"
    abs_target = (tmp_path / "abs_sloppak.txt").resolve()
    evil_name = evil_name_template.format(abs=str(abs_target))

    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", b"title: t\n")
        zf.writestr(evil_name, b"pwned")

    with caplog.at_level(logging.WARNING, logger="slopsmith.lib.sloppak"):
        _unpack_zip(src, dest)

    # Manifest landed inside dest.
    assert (dest / "manifest.yaml").exists()
    # No file escaped the dest root.
    for p in dest.rglob("*"):
        if p.is_file():
            assert dest.resolve() in p.resolve().parents
    assert not (tmp_path / "escaped.txt").exists()
    assert not abs_target.exists()
    if evil_name in (".", "subdir/.."):
        assert any(
            "resolving to unpack root" in r.getMessage() for r in caplog.records
        ), caplog.records


def test_unpack_sloppak_zip_allows_safe_members(tmp_path):
    """Sanity: nested benign entries still extract."""
    from sloppak import _unpack_zip

    src = tmp_path / "good.sloppak"
    dest = tmp_path / "dest"

    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", b"title: t\n")
        zf.writestr("arrangements/lead.json", b"{}")
        zf.writestr("stems/full.ogg", b"oggdata")

    _unpack_zip(src, dest)

    assert (dest / "manifest.yaml").read_bytes().startswith(b"title:")
    assert (dest / "arrangements" / "lead.json").read_bytes() == b"{}"
    assert (dest / "stems" / "full.ogg").read_bytes() == b"oggdata"
