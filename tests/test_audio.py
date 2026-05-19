"""Tests for lib/audio.py command resolution and decode failure reporting.

Some tests below create POSIX shell-script stubs as fake `vgmstream-cli`
binaries. On Windows, `subprocess.run([path])` won't honor the shebang
and `shutil.which` won't pick up an extensionless script as executable,
so those fixtures don't translate cleanly — they get marked
`requires_posix_subprocess` and skipped on Windows. The pure-Python
unit tests (`_scrub_paths`, `_truncate_detail`) deliberately do *not*
carry that marker because the redaction logic they pin is platform-
independent — in particular it has to work on Windows, since that's
where most of the Windows-path inputs come from in real deployments."""

import sys
from pathlib import Path

import pytest

from audio import _scrub_paths, _truncate_detail, _vgmstream_cmd, convert_wem

requires_posix_subprocess = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell-script fixtures are not executable on Windows",
)


# --- _scrub_paths unit tests --------------------------------------------------
#
# These exercise the generic redaction in isolation, so the contract is
# pinned even on platforms where the full convert_wem flow can't run —
# *including* Windows, since the Windows-path branches of the regex are
# specifically meant to handle inputs that originate there. Deliberately
# no `@requires_posix_subprocess` marker on these.

def test_scrub_paths_redacts_posix_path():
    out = _scrub_paths("could not open /etc/secret/file.cfg today")
    assert "/etc/secret" not in out
    assert "file.cfg" in out


def test_scrub_paths_redacts_windows_drive_path():
    out = _scrub_paths(r"could not open C:\Users\Foo\config.txt today")
    assert r"C:\Users" not in out
    assert "config.txt" in out


def test_scrub_paths_redacts_windows_unc_path():
    out = _scrub_paths(r"failed at \\server\share\file.cfg now")
    assert r"\\server" not in out
    assert "file.cfg" in out


def test_scrub_paths_redacts_windows_forward_slash_drive_path():
    """Native Windows APIs and many cross-platform tools (PowerShell,
    .NET, ffmpeg `-i C:/...`) emit drive-qualified paths with forward
    slashes, not backslashes. The regex must catch that shape too."""
    out = _scrub_paths("ffmpeg: could not open C:/Users/Alice/song.wem now")
    assert "C:/Users" not in out
    assert "song.wem" in out


def test_scrub_paths_redacts_quoted_windows_forward_slash_path():
    out = _scrub_paths('failed at "C:/Program Files/vgmstream-cli.exe" again')
    assert "C:/Program" not in out
    assert "Program Files" not in out
    assert "vgmstream-cli.exe" in out


def test_scrub_paths_redacts_quoted_path_with_spaces():
    """Decoders quote paths that contain spaces. The previous regex
    stopped at any whitespace, so `'C:\\Program Files\\foo.exe'` only
    matched the `C:\\Program` prefix and the rest leaked through. The
    quoted-path branch should now match the full quoted span."""
    out = _scrub_paths('failed at "C:\\Program Files\\vgmstream-cli.exe" today')
    assert "C:\\Program" not in out
    assert "Program Files" not in out
    assert "vgmstream-cli.exe" in out


def test_scrub_paths_redacts_quoted_posix_path_with_spaces():
    out = _scrub_paths("could not open '/Users/Alice/My Secrets/file.cfg'")
    assert "My Secrets" not in out
    assert "/Users/Alice" not in out
    assert "file.cfg" in out


def test_scrub_paths_handles_trailing_separator():
    # os.path.basename("/a/b/") == "" — the older replacement function fell
    # back to the full match when basename was empty, leaving the directory
    # intact. The current helper strips trailing separators before taking the
    # basename so a search-dir path like this gets collapsed to its final
    # segment ("secrets") rather than slipping through verbatim.
    out = _scrub_paths("scanning search dir /etc/secrets/ now")
    assert "/etc/secrets" not in out
    assert "secrets" in out


# --- _truncate_detail: skip ffmpeg banner ------------------------------------


def test_truncate_detail_skips_ffmpeg_banner():
    """ffmpeg starts its stderr with a multi-line version / build /
    configuration banner. The actionable error is somewhere after. The
    truncate helper should skip the banner lines and pick the first
    actionable one."""
    stderr = (
        "ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright (c) 2000-2021\n"
        "  built with gcc 11 (Ubuntu 11.2.0-19ubuntu1)\n"
        "  configuration: --prefix=/usr --extra-version=0ubuntu0.22.04.1\n"
        "  libavutil      56. 70.100\n"
        "  libavcodec     58.134.100\n"
        "Input #0, wav, from 'song.wav':\n"
        "song.wem: Invalid data found when processing input\n"
    )
    out = _truncate_detail(stderr)
    assert "ffmpeg version" not in out
    assert "libavutil" not in out
    # Picks the actionable line.
    assert "Invalid data found" in out


def test_truncate_detail_caps_long_lines():
    long_line = "boom: " + ("x" * 1000)
    out = _truncate_detail(long_line, limit=80)
    assert len(out) <= 80
    assert out.startswith("boom:")
    assert out.endswith("…")


# --- _vgmstream_cmd / convert_wem flow ---------------------------------------


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@requires_posix_subprocess
def test_vgmstream_cmd_uses_explicit_env_var(tmp_path, monkeypatch):
    exe = tmp_path / "custom-vgmstream"
    _make_executable(exe, "#!/bin/sh\nexit 0\n")

    monkeypatch.setenv("VGMSTREAM_CLI", str(exe))
    monkeypatch.setenv("PATH", "")

    assert _vgmstream_cmd() == str(exe.resolve())


@requires_posix_subprocess
def test_stale_vgmstream_cli_env_falls_through_to_path_binary(tmp_path, monkeypatch):
    """When VGMSTREAM_CLI points at a stale/non-executable path, the
    resolver must log a warning but still find a working binary on PATH.
    This locks in the "log + fall through" contract — a future change
    that turned the env var into a hard failure would silently break
    desktop users who have a working bundled or PATH binary even
    though their env var got stale."""
    stale = tmp_path / "definitely-not-there"  # never created

    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    on_path = path_dir / "vgmstream-cli"
    _make_executable(on_path, "#!/bin/sh\nexit 0\n")

    monkeypatch.setenv("VGMSTREAM_CLI", str(stale))
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    assert _vgmstream_cmd() == str(on_path.resolve())


@requires_posix_subprocess
def test_vgmstream_cli_env_overrides_path_binary(tmp_path, monkeypatch):
    """VGMSTREAM_CLI must beat a binary on PATH so a user can force a
    known-good build when the system one is broken."""
    override = tmp_path / "override-vgmstream"
    _make_executable(override, "#!/bin/sh\nexit 0\n")

    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    on_path = path_dir / "vgmstream-cli"
    _make_executable(on_path, "#!/bin/sh\nexit 0\n")

    monkeypatch.setenv("VGMSTREAM_CLI", str(override))
    monkeypatch.setenv("PATH", str(path_dir))

    assert _vgmstream_cmd() == str(override.resolve())


@requires_posix_subprocess
def test_vgmstream_cmd_bundled_beats_path_when_env_unset(tmp_path, monkeypatch):
    """With no VGMSTREAM_CLI env, the desktop-bundled binary must still beat
    a `vgmstream-cli` that happens to be on PATH — that's what protects
    desktop users from a system Homebrew/distro binary built without the
    codecs we rely on. Pinned here directly because `_vgmstream_cmd` now
    reimplements the bundled-vs-PATH ordering rather than delegating to
    `_bundled_or_path`, so the generic `_bundled_or_path` tests wouldn't
    catch a regression in this specific resolver."""
    bundle_bin = tmp_path / "resources" / "bin"
    bundle_bin.mkdir(parents=True)
    bundled_exe = bundle_bin / "vgmstream-cli"
    _make_executable(bundled_exe, "#!/bin/sh\nexit 0\n")

    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    on_path = path_dir / "vgmstream-cli"
    _make_executable(on_path, "#!/bin/sh\nexit 0\n")

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: bundle_bin)
    monkeypatch.setenv("PATH", str(path_dir))

    assert _vgmstream_cmd() == str(bundled_exe)


@requires_posix_subprocess
def test_bundled_binary_skipped_when_not_executable(tmp_path, monkeypatch):
    """A bundled binary file that has lost its +x bit (an unpacked tar
    that didn't preserve perms, an over-restrictive umask, etc.) must
    not be returned as the resolved decoder — `_vgmstream_cmd` should
    fall through to PATH and pick the working binary there. Otherwise a
    bad bundle marker silently blocks an otherwise-fine installation."""
    bundle_bin = tmp_path / "resources" / "bin"
    bundle_bin.mkdir(parents=True)
    bundled_exe = bundle_bin / "vgmstream-cli"
    bundled_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    # Deliberately do NOT chmod +x. Bundled file exists but isn't runnable.

    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    on_path = path_dir / "vgmstream-cli"
    _make_executable(on_path, "#!/bin/sh\nexit 0\n")

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: bundle_bin)
    monkeypatch.setenv("PATH", str(path_dir))

    # Should resolve the PATH binary, not the unexecutable bundled one.
    assert _vgmstream_cmd() == str(on_path.resolve())


@requires_posix_subprocess
def test_vgmstream_cli_env_overrides_bundled_binary(tmp_path, monkeypatch):
    """VGMSTREAM_CLI must beat the desktop-bundled binary too, so a user
    with a broken bundled release can still force a known-good build."""
    bundle_bin = tmp_path / "resources" / "bin"
    bundle_bin.mkdir(parents=True)
    bundled_exe = bundle_bin / "vgmstream-cli"
    _make_executable(bundled_exe, "#!/bin/sh\nexit 0\n")

    override = tmp_path / "override-vgmstream"
    _make_executable(override, "#!/bin/sh\nexit 0\n")

    # Fake the bundle-detection so it points at our temp bundle layout.
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: bundle_bin)
    monkeypatch.setenv("VGMSTREAM_CLI", str(override))
    monkeypatch.setenv("PATH", "")

    assert _vgmstream_cmd() == str(override.resolve())


@requires_posix_subprocess
def test_vgmstream_cmd_falls_back_to_repo_local_binary(tmp_path, monkeypatch):
    exe = tmp_path / "vgmstream" / "cli" / "vgmstream-cli"
    exe.parent.mkdir(parents=True)
    _make_executable(exe, "#!/bin/sh\nexit 0\n")

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("audio._repo_root", lambda: tmp_path)
    # Disable the desktop-bundle detection — on a developer machine where
    # `<parents[2]>/bin/vgmstream-cli` happens to exist (the layout
    # _bundled_bin_dir scans for) this assertion would resolve that binary
    # instead of the temp repo-local one and fail for env-dependent reasons.
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    assert _vgmstream_cmd() == str(exe.resolve())


@requires_posix_subprocess
def test_convert_wem_error_scrubs_unrelated_absolute_paths(tmp_path, monkeypatch):
    """If a decoder happens to emit an absolute path the caller never
    passed in (a plugin search path, a system file the decoder couldn't
    open, etc.), the generic-path regex in `_scrub_paths` should redact
    that too. This pins the behavior the explicit-paths pass alone
    couldn't deliver."""
    decoder_dir = tmp_path / "tooldir"
    decoder_dir.mkdir()
    exe = decoder_dir / "vgmstream-cli"
    # Emit an unrelated absolute path the caller doesn't know about.
    _make_executable(
        exe,
        '#!/bin/sh\necho "could not open /etc/unrelated-secret/file.cfg" 1>&2\nexit 2\n',
    )

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setenv("PATH", str(decoder_dir))
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    assert "/etc/unrelated-secret/file.cfg" not in msg
    # Basename of the unrelated path is fine and informative.
    assert "file.cfg" in msg


@requires_posix_subprocess
def test_convert_wem_truncates_huge_decoder_stderr(tmp_path, monkeypatch):
    """A decoder that dumps an enormous multi-line banner on failure
    must not let that whole banner ride along into the `audio_error`
    payload — clients receive these over WebSocket. Each fragment is
    truncated to one line, capped under the per-fragment char limit.

    Uses a pure-shell loop (not `yes | head`) because PATH is narrowed
    to the decoder dir for this test, so external commands wouldn't be
    on the search path."""
    decoder_dir = tmp_path / "tooldir"
    decoder_dir.mkdir()
    exe = decoder_dir / "vgmstream-cli"
    # First line is the actionable error; many subsequent lines are noise.
    body = (
        '#!/bin/sh\n'
        'echo "boom: real reason for failure" 1>&2\n'
        'i=0\n'
        'while [ $i -lt 50 ]; do\n'
        '  echo "noise line $i with filler content to expand the payload" 1>&2\n'
        '  i=$((i + 1))\n'
        'done\n'
        'exit 2\n'
    )
    _make_executable(exe, body)

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setenv("PATH", str(decoder_dir))
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    # The actionable first line survives.
    assert "real reason for failure" in msg
    # The 50 noise lines do NOT — payload is bounded.
    assert "noise line" not in msg
    # Sanity cap on absolute size of the message.
    assert len(msg) < 2000


@requires_posix_subprocess
def test_convert_wem_no_decoder_keeps_install_guidance(tmp_path, monkeypatch):
    """If every decoder is missing entirely (including via a stale
    VGMSTREAM_CLI override), the user-facing error must still be the
    actionable "install vgmstream-cli" guidance — not "Failed to
    decode WEM" with only a resolution note. The stale-override note
    should ride along as a prefix so the misconfigured user understands
    why their override was ignored, but the install guidance is the
    payload."""
    stale = tmp_path / "no-such-binary"

    monkeypatch.setenv("VGMSTREAM_CLI", str(stale))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)
    monkeypatch.setattr("audio._repo_root", lambda: tmp_path / "no-repo-binaries-here")
    # Force ffmpeg/ww2ogg out of the picture too so we hit the no-decoder branch.
    monkeypatch.setattr("audio._ffmpeg_cmd", lambda: None)
    monkeypatch.setattr("audio.shutil.which", lambda name: None)

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    # Install guidance is present.
    assert "Install vgmstream-cli" in msg
    # The stale-override note rides along.
    assert "VGMSTREAM_CLI" in msg
    assert "ignored" in msg
    # We did NOT mislead the user into thinking a decoder ran and failed.
    assert "Failed to decode WEM" not in msg


@requires_posix_subprocess
def test_convert_wem_appends_install_hint_when_only_ffmpeg_tried(tmp_path, monkeypatch):
    """If vgmstream-cli isn't found but ffmpeg is and runs+fails, the
    user must still see the install hint — ffmpeg is commonly present
    on user systems and usually can't decode Wwise WEMs, so without
    this hint a user who actually needs vgmstream-cli sees only
    'unsupported input' and never learns what to install."""
    # No decoder dir, no env, no bundled.
    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)
    monkeypatch.setattr("audio._repo_root", lambda: tmp_path / "no-repo-binaries")

    # Provide a fake ffmpeg that always fails.
    ffmpeg_dir = tmp_path / "ffmpeg-dir"
    ffmpeg_dir.mkdir()
    fake_ffmpeg = ffmpeg_dir / "ffmpeg"
    _make_executable(fake_ffmpeg, '#!/bin/sh\necho "unsupported format" 1>&2\nexit 1\n')
    monkeypatch.setattr("audio._ffmpeg_cmd", lambda: str(fake_ffmpeg))
    # No vgmstream-cli, no ww2ogg on PATH.
    monkeypatch.setenv("PATH", "")

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    # ffmpeg ran and was reported.
    assert "ffmpeg" in msg
    # And the install hint rides along even though a decoder did run.
    # Assert the install-hint *intent* directly — a previous version
    # of this check accepted any mention of "vgmstream-cli", which
    # would still pass if the binary name appeared only in unrelated
    # resolver-note text (e.g. "VGMSTREAM_CLI=foo ignored") and the
    # actionable install line regressed away.
    assert "install vgmstream-cli" in msg.lower()


@requires_posix_subprocess
def test_convert_wem_handles_ffmpeg_oserror(tmp_path, monkeypatch):
    """If ffmpeg launches with OSError (wrong architecture, missing
    loader), convert_wem must catch it like the vgmstream branch does
    so the browser receives the aggregated scrubbed error rather than
    the raw exception, and the ww2ogg fallback below still gets a turn."""
    # Skip vgmstream entirely.
    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)
    monkeypatch.setattr("audio._repo_root", lambda: tmp_path / "no-repo-binaries")
    monkeypatch.setenv("PATH", "")

    # Fake ffmpeg path that subprocess.run will raise OSError on.
    bogus_ffmpeg = "/nonexistent/path/to/ffmpeg-binary"
    monkeypatch.setattr("audio._ffmpeg_cmd", lambda: bogus_ffmpeg)

    # ww2ogg is not installed; we only want to *prove* that the lookup
    # ran, which is the observable evidence that the ffmpeg OSError
    # didn't escape convert_wem and short-circuit the fallback chain.
    ww2ogg_called = []

    def fake_which(name):
        if name == "ww2ogg":
            ww2ogg_called.append(name)
            return None  # not installed
        return None
    monkeypatch.setattr("audio.shutil.which", fake_which)

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    # ffmpeg OSError was caught and reported, not raised raw.
    assert "ffmpeg mp3" in msg or "ffmpeg wav" in msg
    assert "failed to invoke" in msg
    # The absolute bogus_ffmpeg path was scrubbed to its basename.
    assert "/nonexistent/path/to" not in msg
    # ww2ogg lookup ran (proves ffmpeg OSError didn't escape convert_wem).
    assert ww2ogg_called == ["ww2ogg"]


@requires_posix_subprocess
def test_convert_wem_falls_back_to_wav_when_post_vgmstream_ffmpeg_fails(tmp_path, monkeypatch):
    """vgmstream decodes WEM → WAV successfully, then we try to
    transcode WAV → MP3 via ffmpeg. If that ffmpeg invocation raises
    OSError (wrong arch, missing loader), convert_wem must catch it
    and return the WAV — not let the raw exception escape."""
    decoder_dir = tmp_path / "tooldir"
    decoder_dir.mkdir()
    vgmstream_exe = decoder_dir / "vgmstream-cli"
    # Fake vgmstream that "successfully decodes" by creating a non-empty
    # output WAV at the path supplied via -o.
    _make_executable(
        vgmstream_exe,
        '#!/bin/sh\nshift; out=$1; printf "RIFFfakewavdata" > "$out"\nexit 0\n',
    )

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setenv("PATH", str(decoder_dir))
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    # Bogus ffmpeg that subprocess.run will raise OSError on.
    monkeypatch.setattr("audio._ffmpeg_cmd", lambda: "/nonexistent/path/to/ffmpeg-binary")

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")
    out_base = tmp_path / "out"

    # Should NOT raise — should fall back to returning the WAV.
    result = convert_wem(str(wem_path), str(out_base))
    assert result.endswith(".wav")
    assert Path(result).exists()


@requires_posix_subprocess
def test_convert_wem_handles_vgmstream_oserror(tmp_path, monkeypatch):
    """If the resolved vgmstream-cli launches with OSError (wrong
    architecture, missing loader, broken bundle), `_decode_wem_to_wav`
    must catch it so the browser receives the scrubbed aggregated
    error and the ffmpeg / ww2ogg fallbacks still get a turn. This is
    one of the production failure modes this PR is meant to improve
    (a desktop bundle from the wrong arch landing on a user's box)."""
    # Point VGMSTREAM_CLI at a bogus path that resolves *as if* it
    # existed — we don't want to fall through to "stale env" treatment,
    # we want subprocess.run to actually be called with this path and
    # raise OSError. Easiest way is to make the resolver return a path
    # that doesn't exist via a direct monkeypatch of _vgmstream_cmd
    # (bypassing the resolver's exec check).
    bogus_vgmstream = "/nonexistent/path/to/vgmstream-binary"

    def fake_vgmstream_cmd(resolution_notes=None):
        return bogus_vgmstream
    monkeypatch.setattr("audio._vgmstream_cmd", fake_vgmstream_cmd)
    monkeypatch.setattr("audio._ffmpeg_cmd", lambda: None)

    ww2ogg_called = []

    def fake_which(name):
        if name == "ww2ogg":
            ww2ogg_called.append(name)
            return None
        return None
    monkeypatch.setattr("audio.shutil.which", fake_which)

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    # vgmstream OSError was caught, scrubbed, and reported.
    assert "vgmstream" in msg
    assert "failed to invoke" in msg
    # The absolute bogus path got scrubbed.
    assert "/nonexistent/path/to" not in msg
    # The ww2ogg fallback also ran.
    assert ww2ogg_called == ["ww2ogg"]


@requires_posix_subprocess
def test_convert_wem_surfaces_stale_vgmstream_cli_to_user(tmp_path, monkeypatch):
    """When VGMSTREAM_CLI is set but invalid AND no other decoder works,
    the user-facing error must mention that the override was ignored —
    otherwise users see the generic "no decoder found" guidance even
    though they did set the env var the guidance tells them to set."""
    stale = tmp_path / "definitely-not-there"  # never created

    monkeypatch.setenv("VGMSTREAM_CLI", str(stale))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)
    monkeypatch.setattr("audio._repo_root", lambda: tmp_path / "no-repo-binaries-here")

    wem_path = tmp_path / "song.wem"
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    assert "VGMSTREAM_CLI" in msg
    assert "ignored" in msg
    # But the absolute env value itself must not leak.
    assert str(stale) not in msg


@requires_posix_subprocess
def test_convert_wem_error_does_not_leak_absolute_paths(tmp_path, monkeypatch):
    """convert_wem's RuntimeError is surfaced to the browser as
    `audio_error`. Make sure neither the wem input path nor the decoder
    binary path appear verbatim in that error — leaking install/user
    paths to the client is needless info disclosure."""
    decoder_dir = tmp_path / "private" / "secret-install"
    decoder_dir.mkdir(parents=True)
    exe = decoder_dir / "vgmstream-cli"
    _make_executable(exe, '#!/bin/sh\necho "failed to read /unrelated/private/file"\nexit 2\n')

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setenv("PATH", str(decoder_dir))
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    wem_path = tmp_path / "user-dir" / "deep" / "song_p.wem"
    wem_path.parent.mkdir(parents=True)
    wem_path.write_bytes(b"")

    with pytest.raises(RuntimeError) as ei:
        convert_wem(str(wem_path), str(tmp_path / "out"))

    msg = str(ei.value)
    # The basename of the wem is informative and OK to keep.
    assert "song_p.wem" in msg
    # The full input path must not appear.
    assert str(wem_path) not in msg
    # The decoder install path must not appear either.
    assert str(decoder_dir) not in msg
    assert str(exe) not in msg


@requires_posix_subprocess
def test_convert_wem_reports_decode_failure_not_missing_decoder(tmp_path, monkeypatch):
    exe = tmp_path / "vgmstream-cli"
    _make_executable(exe, "#!/bin/sh\necho boom 1>&2\nexit 1\n")

    monkeypatch.delenv("VGMSTREAM_CLI", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    # Same isolation concern as above — _vgmstream_cmd checks the bundled
    # location first, so a developer-machine `<parents[2]>/bin/vgmstream-cli`
    # would beat our PATH-resident fake and make convert_wem use the real
    # binary, which won't produce the "boom" error we're asserting on.
    monkeypatch.setattr("audio._bundled_bin_dir", lambda: None)

    with pytest.raises(RuntimeError, match=r"Failed to decode WEM .*vgmstream: boom"):
        convert_wem(str(tmp_path / "missing.wem"), str(tmp_path / "out"))
