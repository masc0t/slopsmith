"""Unit tests for ``lib/safepath.py::safe_join``.

The three archive extractors and the filename-bound HTTP routes all rely on
this helper for containment. Pin the contract here so any regression surfaces
at the helper layer before it can sneak into a call site."""

from __future__ import annotations

from pathlib import Path

import pytest

from safepath import safe_join


@pytest.fixture()
def root(tmp_path):
    return tmp_path


# ── Rejected inputs ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "",                               # empty
        "..",                             # bare dotdot
        "../escaped",                     # one-level traversal
        "../" * 6 + "tmp/pwn",            # deep traversal
        "subdir/../../escaped",           # mid-path traversal
        "/etc/passwd",                    # absolute POSIX
        "..\\windows",                    # backslash-traversal (Windows-style)
        "\x00null",                       # NUL byte → ValueError on resolve
    ],
)
def test_safe_join_rejects(root, name):
    assert safe_join(root, name) is None


# ── Accepted inputs ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "file.txt",
        "subdir/file.txt",
        "deep/nested/dir/file.txt",
        "a/./b",                          # current-dir segment, contained
        "%2e%2e/x",                       # literal percent-encoding (not decoded)
    ],
)
def test_safe_join_accepts(root, name):
    result = safe_join(root, name)
    assert result is not None
    assert result.is_relative_to(root.resolve())


def test_safe_join_dot_resolves_to_root(root):
    """A bare ``.`` is contained (equals root). Caller is responsible for
    declining to write a file at the root path."""
    result = safe_join(root, ".")
    assert result == root.resolve()


def test_safe_join_returns_resolved_path(root):
    """Result is fully resolved — caller can use ``is_relative_to`` directly
    without re-resolving."""
    (root / "sub").mkdir()
    result = safe_join(root, "sub/file.txt")
    assert result == (root / "sub" / "file.txt").resolve()


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create ``link`` pointing at ``target``, or skip the test on platforms
    where symlinks aren't allowed (Windows without dev-mode, restricted
    container, non-symlink-capable filesystem).

    Passes ``target_is_directory=target.is_dir()`` so directory symlinks work
    on Windows hosts that *do* allow symlinks — otherwise this helper would
    skip unnecessarily on Windows even when the underlying capability is
    present.
    """
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks not available on this platform: {e}")


def test_safe_join_symlinked_root_still_contains(tmp_path):
    """Containment uses realpath on both sides so a symlinked root doesn't
    break the check."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _symlink_or_skip(link, real)
    result = safe_join(link, "file.txt")
    assert result is not None
    assert result == (real / "file.txt").resolve()


def test_safe_join_traversal_through_symlinked_root(tmp_path):
    """A ``..`` from a symlinked root must still resolve outside the real
    root (not just the symlink path) and be rejected."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _symlink_or_skip(link, real)
    assert safe_join(link, "../escaped") is None
