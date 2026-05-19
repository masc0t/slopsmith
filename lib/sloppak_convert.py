"""PSARC → sloppak conversion + stem splitting.

This module is the single source of truth for the convert + split pipelines.
Both the CLI scripts (`scripts/psarc_to_sloppak.py`, `scripts/split_stems.py`)
and the in-app converter plugin (`plugins/sloppak_converter`) import from
here — see the plugin's `routes.py` for the job queue that wraps these
functions with progress reporting.

Each function accepts a `progress_cb(fraction: float, stage: str, message: str)`
callback that the job queue forwards to the client over a WebSocket.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("slopsmith.lib.sloppak_convert")

import yaml

from patcher import unpack_psarc
from song import load_song, arrangement_to_wire
from tones import extract_tones_for_song
from audio import find_wem_files, _vgmstream_cmd, _ffmpeg_cmd, _ffmpeg_wav_to_ogg


ProgressCB = Optional[Callable[[float, str, str], None]]


# Prefix shared by every staging directory this module creates:
# `s2p_extract_`, `s2p_work_`, `s2p_wem_`, `s2p_split_`, `s2p_split_zip_`.
# `cleanup_stale_temp_dirs()` keys off this so a single sweep covers
# everything we leak when the host process is killed mid-conversion.
_TEMP_DIR_PREFIX = "s2p_"


# ── Shared helpers ────────────────────────────────────────────────────────────

def sanitize_stem(name: str) -> str:
    """Filesystem-safe version of a filename stem."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return s or "song"


def _progress(cb: ProgressCB, frac: float, stage: str, msg: str) -> None:
    if cb:
        try:
            cb(frac, stage, msg)
        except Exception as e:
            log.debug("progress callback raised: %s", e)


def _arrangement_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "arr"
    candidate = base
    i = 2
    while candidate in used:
        candidate = f"{base}{i}"
        i += 1
    used.add(candidate)
    return candidate


def _wem_to_ogg(wem_path: str, out_ogg: Path) -> None:
    vgmstream = _vgmstream_cmd()
    ffmpeg = _ffmpeg_cmd()
    if not vgmstream:
        raise RuntimeError("vgmstream-cli not found on PATH")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    with tempfile.TemporaryDirectory(prefix="s2p_wem_") as td:
        wav = Path(td) / "full.wav"
        r = subprocess.run([vgmstream, "-o", str(wav), wem_path], capture_output=True)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 100:
            raise RuntimeError(
                f"vgmstream-cli failed: {r.stderr.decode(errors='replace')}"
            )
        out_ogg.parent.mkdir(parents=True, exist_ok=True)
        r2 = _ffmpeg_wav_to_ogg(ffmpeg, wav, out_ogg)
        if r2.returncode != 0 or not out_ogg.exists() or out_ogg.stat().st_size < 100:
            raise RuntimeError(
                f"ffmpeg OGG encode failed: {r2.stderr.decode(errors='replace')}"
            )


def _parse_lyrics(extracted_dir: Path) -> list[dict]:
    # Try vocals XML first (CDLC and some official DLC)
    for xml_path in sorted(extracted_dir.rglob("*.xml")):
        try:
            root = ET.parse(xml_path).getroot()
        except Exception as e:
            log.debug("lyrics XML parse error in %s: %s", xml_path.name, e)
            continue
        if root.tag != "vocals":
            continue
        return [
            {
                "t": round(float(v.get("time", "0")), 3),
                "d": round(float(v.get("length", "0")), 3),
                "w": v.get("lyric", ""),
            }
            for v in root.findall("vocal")
        ]
    # Fall back to vocals SNG (official DLC ships SNG-only)
    try:
        from sng_vocals import parse_vocals_sng
        for sng_path in sorted(extracted_dir.rglob("*vocals*.sng")):
            plat = "mac" if "/macos/" in str(sng_path).replace("\\", "/").lower() else "pc"
            lyrics = parse_vocals_sng(str(sng_path), plat)
            if lyrics:
                return lyrics
    except ImportError:
        pass
    return []


def _extract_cover(extracted_dir: Path, out_jpg: Path) -> bool:
    dds_files = sorted(
        extracted_dir.rglob("*.dds"), key=lambda p: p.stat().st_size, reverse=True
    )
    if not dds_files:
        return False
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        img = Image.open(dds_files[0]).convert("RGB")
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_jpg), "JPEG", quality=88)
        return True
    except Exception as e:
        log.debug("cover art extraction failed: %s", e)
        return False


def _zip_dir(src_dir: Path, out_zip: Path) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(out_zip), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in src_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src_dir).as_posix())


def _newest_mtime_within(dir_path: Path) -> float | None:
    """Return the most recent mtime found anywhere inside `dir_path`
    (including the directory itself), or ``None`` if:

    * ``stat()`` failed on the directory entry itself, or
    * the recursive ``rglob`` walk raised an ``OSError`` mid-iteration.

    Callers treat ``None`` as "unknown activity — do not delete".
    Returning a partial mtime when the walk was interrupted is unsafe:
    traversal may have stopped before reaching a recently-written leaf,
    causing an under-estimate that would incorrectly classify an
    in-flight staging dir as stale.

    Directory mtime on its own is not a reliable activity signal —
    it advances when direct children are added/removed/renamed but
    NOT when files deeper in the tree are written. A Demucs job
    writing under `s2p_split_xxx/htdemucs_6s/<track>/` looks idle
    from the top directory's perspective even mid-run. Walking
    `rglob("*")` and taking the max mtime gives us a real
    "any descendant touched recently" signal. Staging dirs in this
    module hold tens to a few hundred files at most, so the walk
    is cheap; individual entries that fail to stat are skipped
    rather than aborting the whole comparison."""
    try:
        newest = dir_path.stat().st_mtime
    except OSError:
        return None
    try:
        for child in dir_path.rglob("*"):
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    except OSError:
        # rglob can raise mid-iteration on a transient I/O error.
        # Returning the partial mtime accumulated so far is unsafe:
        # if traversal stopped before reaching a recently-written leaf
        # we would under-estimate the newest mtime and could delete an
        # in-flight staging dir. Returning None signals the caller to
        # skip this entry entirely (treat unknown activity as active).
        log.debug("_newest_mtime_within: walk of %s failed, treating as active", dir_path)
        return None
    return newest


def cleanup_stale_temp_dirs(min_age_seconds: float = 0.0) -> int:
    """Sweep `tempfile.gettempdir()` of orphaned `s2p_*` staging dirs left
    behind by killed conversions.

    Each convert / split routine in this module wraps its work in a
    `tempfile.mkdtemp` / `tempfile.TemporaryDirectory` whose prefix
    starts with `_TEMP_DIR_PREFIX` (the per-routine prefixes are the
    specific strings `s2p_extract_`, `s2p_work_`, `s2p_wem_`,
    `s2p_split_`, and `s2p_split_zip_` — the shared `s2p_` head is
    what we match against here so a single sweep covers them all).
    Cleanup of those dirs relies on a `finally` / context-manager
    `__exit__`. Those guarantees do NOT hold if the host process is
    SIGKILL'd (Docker shutdown timeout, OOM kill, `docker compose
    restart` while a job is mid-flight), and the dirs accumulate
    forever. A bulk-convert run that's restarted a few times can
    leave many GB of leftover PSARC extractions under `/tmp` on a
    long-lived container.

    Intended to be called once at host startup, BEFORE any new
    conversion runs in *this* process. Note that startup of this
    process does NOT guarantee filesystem exclusivity:

    - A second slopsmith instance can share `tempfile.gettempdir()`
      (containers writing to a host-mounted `/tmp`, two instances
      run on the same workstation, rolling-restart deployments
      where the old and new processes overlap briefly).
    - An external cleanup pass (cron `tmpwatch`, container init
      hooks) can race with this one.

    `min_age_seconds` is the safety knob for that. The "age" signal
    is the most recent mtime found anywhere inside the staging dir
    (recursive walk), NOT the top-level dir's own mtime — directory
    mtime only advances when direct children are created / removed
    / renamed, so a long Demucs run that writes under
    `s2p_split_xxx/htdemucs_6s/track/` would leave the top dir's
    own mtime stale even while the job is actively writing.
    Recursive `rglob` over a staging dir is cheap (~tens to a few
    hundred files for a real conversion) and gives us a reliable
    "any descendant touched within the threshold" gate.

    Pass a value safely larger than the longest stretch of "no
    visible writes anywhere in the tree" a live conversion might
    have. Server startup hands in 900s (15 minutes), which covers
    the remote Demucs polling window (up to 10 minutes with no
    file writes while the server-side job runs) plus margin for
    upload and download time. For local Demucs / PSARC / WEM
    routines that write continuously, the recursive mtime check
    keeps active dirs alive regardless of threshold.
    The default of 0 is appropriate only for callers who can prove
    filesystem exclusivity (e.g. test harnesses with isolated
    `tmp_path` fixtures).

    For truly hostile shared-`/tmp` environments, the safer answer
    is a per-instance temp root (override via `TMPDIR`); this
    helper is a best-effort backstop, not a substitute.

    Returns the number of directories removed."""
    temp_root = Path(tempfile.gettempdir())
    if not temp_root.is_dir():
        return 0

    import time
    now = time.time()
    removed = 0
    # Stream the directory listing rather than materialize the whole
    # set up front — `/tmp` can hold many thousands of entries on a
    # busy host, and we only ever look at one at a time. `iterdir()`
    # returns a lazy generator, so an `OSError` from the underlying
    # `scandir` can be raised either at construction OR mid-iteration
    # (e.g., the temp dir is unmounted while we're walking it). Wrap
    # the whole `for` so either path lands the helper on the same
    # graceful "give up and report what we got" branch — startup must
    # not crash because /tmp had a transient hiccup.
    try:
        for entry in temp_root.iterdir():
            if not entry.name.startswith(_TEMP_DIR_PREFIX):
                continue
            # Skip files / symlinks — we only ever create directories with
            # this prefix, so anything else under that name is foreign.
            if entry.is_symlink() or not entry.is_dir():
                continue
            if min_age_seconds > 0:
                newest = _newest_mtime_within(entry)
                if newest is None:
                    # Stat failed entirely — safer to skip than to delete
                    # a directory we can't read; the next sweep can retry.
                    continue
                if (now - newest) < min_age_seconds:
                    continue
            try:
                shutil.rmtree(entry, ignore_errors=False)
                removed += 1
            except FileNotFoundError:
                # Race: another process / a concurrent startup removed the
                # directory between `iterdir()` and the `rmtree`. Benign —
                # the end state is what we wanted anyway, so DEBUG only.
                log.debug("cleanup_stale_temp_dirs: %s vanished before removal", entry)
            except OSError as e:
                # A locked file on Windows or a permissions hiccup
                # shouldn't crash startup; log at WARNING and move on so
                # operators can see real failures (vs the benign race
                # above) in the log.
                log.warning("cleanup_stale_temp_dirs: could not remove %s: %s", entry, e)
    except OSError as e:
        # `iterdir()` / `scandir` failed either at construction or
        # mid-iteration. Whatever we already removed stays counted;
        # the next startup pass will retry.
        log.debug("cleanup_stale_temp_dirs: listing %s failed: %s", temp_root, e)
    if removed:
        log.info("cleanup_stale_temp_dirs: removed %d orphaned dir(s) under %s",
                 removed, temp_root)
    return removed


def _remove_path(p: Path) -> None:
    """Remove `p` whether it is a file or a directory; no-op if absent.

    Sloppak outputs can be either zip-form (file) or dir-form
    (directory), so staging / backup paths next to them may need to
    survive crossing between the two forms — e.g. a leftover
    `<out>.sloppak.tmp` file from a killed zip-form convert getting
    cleaned up before a fresh `as_dir=True` convert stages its own
    directory at the same path. Using a single helper keeps the call
    sites symmetric and prevents `NotADirectoryError` /
    `IsADirectoryError` from a mismatched cleanup primitive."""
    if p.is_symlink():
        # Symlinks should never appear inside our staging paths, but if
        # one does, drop the link itself rather than follow it.
        p.unlink(missing_ok=True)
        return
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink(missing_ok=True)


# ── PSARC → sloppak ───────────────────────────────────────────────────────────

def convert_psarc_to_sloppak(
    psarc_path: Path,
    out_path: Path,
    as_dir: bool = False,
    progress_cb: ProgressCB = None,
) -> Path:
    """Convert a PSARC to a .sloppak (single-stem). Returns the output path."""
    _progress(progress_cb, 0.02, "extracting", f"Unpacking {psarc_path.name}")
    tmp_extract = Path(tempfile.mkdtemp(prefix="s2p_extract_"))
    work_dir = Path(tempfile.mkdtemp(prefix="s2p_work_"))
    try:
        unpack_psarc(str(psarc_path), str(tmp_extract))

        _progress(progress_cb, 0.15, "extracting", "Parsing song data")
        song = load_song(str(tmp_extract))
        if not song.arrangements:
            raise RuntimeError("no playable arrangements found in PSARC")

        # Lift tone data (gear definitions + in-song tone changes) out of the
        # unpacked PSARC once — PSARCs keep tones in the manifest JSON /
        # arrangement XML, neither of which survives into the sloppak, so
        # without this the converted sloppak loses all tones. Done in one
        # pass (not per arrangement) to avoid re-scanning the extracted tree.
        try:
            tones_by_arr = extract_tones_for_song(
                tmp_extract, [a.name for a in song.arrangements]
            )
        except Exception as e:
            log.warning("tone extraction failed: %s", e, exc_info=True)
            tones_by_arr = {}

        used_ids: set[str] = set()
        arr_manifest: list[dict] = []
        first = True
        for arr in song.arrangements:
            aid = _arrangement_id(arr.name, used_ids)
            # Attach tones to the Arrangement so arrangement_to_wire owns the
            # serialization (single source of truth for the wire schema).
            arr.tones = tones_by_arr.get(arr.name)
            wire = arrangement_to_wire(arr)
            if first:
                wire["beats"] = [
                    {"time": round(b.time, 3), "measure": b.measure} for b in song.beats
                ]
                wire["sections"] = [
                    {"name": s.name, "number": s.number, "time": round(s.start_time, 3)}
                    for s in song.sections
                ]
                first = False
            arr_file = work_dir / "arrangements" / f"{aid}.json"
            arr_file.parent.mkdir(parents=True, exist_ok=True)
            arr_file.write_text(json.dumps(wire, separators=(",", ":")), encoding="utf-8")
            arr_manifest.append({
                "id": aid,
                "name": arr.name,
                "file": f"arrangements/{aid}.json",
                "tuning": list(arr.tuning),
                "capo": arr.capo,
            })

        _progress(progress_cb, 0.35, "extracting", "Converting audio (WEM → OGG)")
        wems = find_wem_files(str(tmp_extract))
        if not wems:
            raise RuntimeError("no WEM audio found in PSARC")
        _wem_to_ogg(wems[0], work_dir / "stems" / "full.ogg")

        stems_manifest = [{"id": "full", "file": "stems/full.ogg", "default": "on"}]

        lyrics = _parse_lyrics(tmp_extract)
        lyrics_rel = None
        if lyrics:
            (work_dir / "lyrics.json").write_text(
                json.dumps(lyrics, separators=(",", ":")), encoding="utf-8"
            )
            lyrics_rel = "lyrics.json"

        cover_rel = None
        if _extract_cover(tmp_extract, work_dir / "cover.jpg"):
            cover_rel = "cover.jpg"

        manifest: dict = {
            "title": song.title or psarc_path.stem,
            "artist": song.artist or "",
            "album": song.album or "",
            "year": int(song.year or 0),
            "duration": round(float(song.song_length or 0.0), 3),
        }
        if cover_rel:
            manifest["cover"] = cover_rel
        manifest["stems"] = stems_manifest
        manifest["arrangements"] = arr_manifest
        if lyrics_rel:
            manifest["lyrics"] = lyrics_rel
        (work_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        _progress(progress_cb, 0.85, "packing", "Writing output")
        # Atomic write: build the output at a sibling `.tmp` path first,
        # then move/rename onto `out_path`. Without this, a kill mid-write
        # leaves a partial / truncated `.sloppak` (or worse, a half-deleted
        # dir-form output) on disk; the host's library scan keys off the
        # filename, so the broken file shows up as a "real" sloppak until
        # the next successful re-conversion overwrites it.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out_path.with_name(out_path.name + ".tmp")
        # Pre-clean stale `.tmp` from a previously-killed convert.
        # `_remove_path` handles either form, so a zip-form `.tmp` left
        # behind by an earlier `as_dir=False` crash doesn't block a new
        # `as_dir=True` stage (or vice versa).
        _remove_path(tmp_out)
        if as_dir:
            shutil.copytree(work_dir, tmp_out)
            # Directory rename-onto-existing isn't portable (`os.replace`
            # raises on Windows when dst is a non-empty dir, and POSIX
            # `rename(2)` only swaps empty dirs). Two-step swap via a
            # `.old` sidecar so the failure window is bounded to one
            # rename; on Windows the dst-exists case is still a brief
            # absence rather than a partial dir.
            backup = out_path.with_name(out_path.name + ".old")
            # Backup slot may pre-exist as either file or dir — could
            # be a leftover from a prior killed `as_dir=True` swap, or
            # a stray file a user dropped there. Clear either form.
            _remove_path(backup)
            # `out_path` itself may pre-exist as either form (user
            # reconverting `as_dir=True` over a previous zip-form
            # sloppak, or vice versa). `rename` on a file works the
            # same as on a dir, so no type sniff needed.
            if out_path.exists():
                out_path.rename(backup)
            try:
                tmp_out.rename(out_path)
            except Exception:
                if backup.exists():
                    backup.rename(out_path)
                raise
            # Backup may itself be either form (we just renamed
            # whatever was at out_path into it); use the helper.
            _remove_path(backup)
        else:
            _zip_dir(work_dir, tmp_out)
            # `os.replace` can swap file-onto-file atomically, but
            # cannot replace a non-empty directory with a file (POSIX
            # `rename(2)` returns ENOTDIR/EISDIR; Windows fails the
            # same way). If `out_path` is a dir-form sloppak left from
            # a prior `as_dir=True` convert, clear it first. The brief
            # absence window between rmtree and os.replace mirrors the
            # dir→dir swap path's bounded gap; without this the convert
            # would crash and the user would have to manually delete
            # the dir to recover.
            if out_path.is_dir():
                _remove_path(out_path)
            os.replace(tmp_out, out_path)

        _progress(progress_cb, 1.0, "done", f"Wrote {out_path.name}")
        return out_path
    finally:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        # Clean up staging sidecars left behind if we bailed before the
        # rename. The happy path already moved `.tmp` onto `out_path`
        # and removed `.old`, so these are no-ops there. The `.old`
        # leg matters specifically for kills after `out_path.rename(backup)`
        # but before `tmp_out.rename(out_path)` in the `as_dir=True` path
        # — without this, a stale `.old` dir accumulates next to the
        # (re-created) `out_path` across crashes.
        for sidecar in (
            out_path.with_name(out_path.name + ".tmp"),
            out_path.with_name(out_path.name + ".old"),
        ):
            _remove_path(sidecar)


# ── Stem splitting via Demucs ────────────────────────────────────────────────

_STEM_ORDER = ["guitar", "bass", "drums", "vocals", "piano", "other"]


def demucs_available() -> bool:
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def _get_demucs_server_url() -> str | None:
    """Get the configured remote demucs server URL, if any."""
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    config_file = config_dir / "config.json"
    if config_file.exists():
        try:
            import json
            cfg = json.loads(config_file.read_text())
            url = cfg.get("demucs_server_url", "")
            if url:
                return url.rstrip("/")
        except Exception as e:
            log.debug("failed to read demucs_server_url from config: %s", e)
    return None


def _run_demucs_remote(full_ogg: Path, out_dir: Path, model: str) -> Path:
    """Run stem separation via remote demucs server."""
    import json
    import requests

    server_url = _get_demucs_server_url()
    if not server_url:
        raise RuntimeError("No demucs server configured")

    # Upload the audio file — request all stems the model can produce
    stem_list = "drums,bass,vocals,other,guitar,piano"
    with open(full_ogg, "rb") as f:
        resp = requests.post(
            f"{server_url}/separate",
            files={"file": (full_ogg.name, f, "audio/ogg")},
            params={"model": model, "stems": stem_list},
            timeout=600,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Demucs server error: {resp.text[:300]}")

    data = resp.json()
    stems = data.get("stems", {})
    if not stems:
        # Might be a job-based response — poll for completion
        job_id = data.get("job_id")
        if job_id:
            import time
            for _ in range(120):  # Wait up to 10 minutes
                time.sleep(5)
                jr = requests.get(f"{server_url}/jobs/{job_id}", timeout=30)
                jd = jr.json()
                if jd.get("status") == "complete":
                    stems = jd.get("stems", {})
                    break
                elif jd.get("status") == "failed":
                    raise RuntimeError(f"Demucs server job failed: {jd.get('error')}")

    if not stems:
        raise RuntimeError("Demucs server returned no stems")

    # Download each stem
    result_dir = out_dir / "remote_stems"
    result_dir.mkdir(parents=True, exist_ok=True)
    for stem_name, stem_url in stems.items():
        if stem_url.startswith("/"):
            stem_url = f"{server_url}{stem_url}"
        sr = requests.get(stem_url, timeout=120)
        if sr.status_code == 200:
            ext = ".mp3" if ".mp3" in stem_url else ".wav"
            (result_dir / f"{stem_name}{ext}").write_bytes(sr.content)

    return result_dir


def _run_demucs(full_ogg: Path, out_dir: Path, model: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    config_dir = env.get("CONFIG_DIR", "/config")
    cache_root = Path(config_dir) / "torch_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    env.setdefault("TORCH_HOME", str(cache_root))
    env.setdefault("XDG_CACHE_HOME", str(cache_root))
    # Pin demucs to the bundled ffmpeg/ffprobe. demucs.audio probes the
    # child's PATH for both binaries (ffprobe first, for stream metadata)
    # and falls back to torchcodec when missing; the desktop bundle's
    # torchcodec native shims can't load against the vgmstream-patched
    # FFmpeg DLLs we ship, so the fallback path is broken — we have to
    # keep demucs on the ffmpeg/ffprobe path. Resolves to resources/bin/
    # in desktop builds (lib/sloppak_convert.py → resources/slopsmith/lib
    # → resources/bin). Gate on vgmstream-cli's presence so we don't
    # accidentally prepend a system /bin/ (Docker's `/app/lib/...`
    # resolves parents[2] to `/`, and `/bin/ffprobe` exists there too) —
    # vgmstream-cli is bundled on every desktop platform and isn't a
    # typical system binary, so it's a precise signature for the
    # desktop layout.
    _bundled_bin = Path(__file__).resolve().parents[2] / "bin"
    if any((_bundled_bin / name).is_file() for name in ("vgmstream-cli", "vgmstream-cli.exe")):
        # On Windows the env var is conventionally `Path`, not `PATH`;
        # os.environ is case-insensitive but os.environ.copy() returns a
        # plain dict that preserves whatever casing the OS used. If we
        # blindly write to env["PATH"], Windows ends up with both `Path`
        # and `PATH` keys in the spawned subprocess's env block — and
        # which one wins is implementation-defined. Reuse the existing
        # key's casing (or fall through to "PATH" on Linux/macOS).
        _path_key = next(
            (k for k in env if k.upper() == "PATH"),
            "PATH",
        )
        # Avoid producing a trailing separator when the parent PATH is
        # empty/missing — on some platforms a trailing pathsep implicitly
        # injects the current directory into the search path.
        _existing_path = env.get(_path_key, "")
        env[_path_key] = (
            str(_bundled_bin) + os.pathsep + _existing_path
            if _existing_path
            else str(_bundled_bin)
        )
    # Propagate in-process sys.path additions (plugin loader adds
    # /config/pip_packages at runtime, not via PYTHONPATH) so the child
    # python can also find demucs/torch/torchcodec. PYTHONPATH alone
    # is insufficient on Windows embeddable Python, where the ._pth
    # file forces isolated mode and the env var is ignored — so we
    # also inject sys.path explicitly via a -c bootstrap before
    # delegating to demucs.
    pip_target = str(Path(config_dir) / "pip_packages")
    extra_paths = [p for p in sys.path if p]
    if pip_target not in extra_paths:
        extra_paths.insert(0, pip_target)
    merged = os.pathsep.join(
        extra_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["PYTHONPATH"] = merged
    # torchaudio>=2.11 routes .save() through save_with_torchcodec, which
    # requires torchcodec. The desktop bundle's torchcodec native shims can't
    # load against the vgmstream-patched FFmpeg DLLs we ship (see PATH-prepend
    # rationale above), so the save call dies with OSError; on installs where
    # torchcodec was dropped from requirements, .save() raises ImportError.
    # Redirect .save() to soundfile before demucs imports so demucs's per-stem
    # WAV writes work regardless of torchcodec's state. soundfile is NOT a
    # transitive demucs dep — the in-app converter plugin (sloppak_converter
    # >= 1.0.4) ships it via its own requirements.txt, and any other consumer
    # of this module must do the same. The override stays in place even on
    # torchaudio versions that wouldn't need it — soundfile's WAV writes are
    # behaviorally equivalent for demucs's float32 outputs.
    bootstrap = (
        "import sys, json, runpy\n"
        "sys.path[:0] = json.loads(sys.argv[1])\n"
        "sys.argv = [sys.argv[0]] + sys.argv[2:]\n"
        "import torchaudio as _ta, soundfile as _sf, numpy as _np\n"
        "def _ta_save(uri, src, sample_rate, *_a, **_kw):\n"
        # Honor channels_first (torchaudio default True). demucs calls with
        # the default; third-party callers may not.
        "    _cf = _kw.pop('channels_first', True)\n"
        "    a = src.detach().cpu().numpy() if hasattr(src, 'detach') else _np.asarray(src)\n"
        "    if a.ndim == 2 and _cf: a = a.T\n"
        # Pick subtype that preserves bit depth: float32 -> FLOAT,
        # float64 -> DOUBLE (avoid silent 32-bit downcast), int32 ->
        # PCM_32, otherwise PCM_16.
        "    if a.dtype == _np.float64:\n"
        "        _st = 'DOUBLE'\n"
        "    elif a.dtype.kind == 'f':\n"
        "        _st = 'FLOAT'\n"
        "    elif a.dtype == _np.int32:\n"
        "        _st = 'PCM_32'\n"
        "    else:\n"
        "        _st = 'PCM_16'\n"
        "    _sf.write(str(uri), a, int(sample_rate), subtype=_st)\n"
        "_ta.save = _ta_save\n"
        "runpy.run_module('demucs.__main__', run_name='__main__', alter_sys=True)\n"
    )
    cmd = [sys.executable, "-c", bootstrap, json.dumps(extra_paths),
           "-n", model, "-o", str(out_dir), str(full_ogg)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        # demucs writes loader errors to stdout, not stderr -- include both
        # so the surfaced RuntimeError actually points at the cause.
        out_tail = (r.stdout or "").strip().splitlines()[-8:]
        err_tail = (r.stderr or "").strip().splitlines()[-8:]
        tail = " | ".join(out_tail + err_tail) or "(no output)"
        raise RuntimeError(
            f"demucs exited with code {r.returncode}: " + tail
        )
    track_stem = full_ogg.stem
    result_dir = out_dir / model / track_stem
    if not result_dir.exists():
        candidates = list((out_dir / model).iterdir()) if (out_dir / model).exists() else []
        if len(candidates) == 1 and candidates[0].is_dir():
            result_dir = candidates[0]
        else:
            raise RuntimeError(f"demucs output dir not found under {out_dir}/{model}")
    return result_dir


def _encode_ogg(wav_path: Path, ogg_path: Path) -> None:
    ffmpeg = _ffmpeg_cmd() or "ffmpeg"
    ogg_path.parent.mkdir(parents=True, exist_ok=True)
    r = _ffmpeg_wav_to_ogg(ffmpeg, wav_path, ogg_path)
    if r.returncode != 0 or not ogg_path.exists():
        raise RuntimeError(
            f"ffmpeg OGG encode failed for {wav_path.name}: "
            f"{r.stderr.decode(errors='replace')}"
        )


def _rewrite_stems_manifest(source_dir: Path, new_stems: list[dict]) -> None:
    mf = source_dir / "manifest.yaml"
    if not mf.exists():
        mf = source_dir / "manifest.yml"
    data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    data["stems"] = new_stems
    mf.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _split_in_dir(
    source_dir: Path,
    model: str,
    progress_cb: ProgressCB,
    base_frac: float,
    span_frac: float,
) -> None:
    full_ogg = source_dir / "stems" / "full.ogg"
    if not full_ogg.exists():
        raise FileNotFoundError(
            f"{full_ogg} not found — run PSARC conversion first or add stems/full.ogg."
        )

    # Try remote demucs server first, fall back to local
    remote_url = _get_demucs_server_url()
    use_remote = remote_url is not None

    if use_remote:
        _progress(progress_cb, base_frac + span_frac * 0.05, "splitting",
                  f"Sending to Demucs server ({remote_url})")
    else:
        _progress(progress_cb, base_frac + span_frac * 0.05, "splitting",
                  f"Running Demucs locally ({model})")

    with tempfile.TemporaryDirectory(prefix="s2p_split_") as td:
        if use_remote:
            try:
                result_dir = _run_demucs_remote(full_ogg, Path(td), model)
            except Exception as e:
                log.warning("Demucs remote failed (%s), falling back to local", e)
                if demucs_available():
                    result_dir = _run_demucs(full_ogg, Path(td), model)
                else:
                    raise RuntimeError(f"Remote demucs failed and local demucs not available: {e}")
        else:
            result_dir = _run_demucs(full_ogg, Path(td), model)

        _progress(progress_cb, base_frac + span_frac * 0.85, "splitting",
                  "Encoding split stems")
        produced: list[dict] = []
        stems_dir = source_dir / "stems"
        for wav in sorted(result_dir.glob("*.wav")):
            name = wav.stem.lower()
            out_ogg = stems_dir / f"{name}.ogg"
            _encode_ogg(wav, out_ogg)
            produced.append({"id": name, "file": f"stems/{name}.ogg", "default": "on"})

    if not produced:
        raise RuntimeError("demucs produced no output stems")

    def _order_key(s: dict) -> tuple[int, str]:
        try:
            return (_STEM_ORDER.index(s["id"]), s["id"])
        except ValueError:
            return (len(_STEM_ORDER), s["id"])
    produced.sort(key=_order_key)

    full_ogg.unlink(missing_ok=True)
    _rewrite_stems_manifest(source_dir, produced)


def split_sloppak_stems(
    sloppak_path: Path,
    model: str = "htdemucs_6s",
    progress_cb: ProgressCB = None,
    base_frac: float = 0.0,
    span_frac: float = 1.0,
) -> None:
    """Split a sloppak's stems/full.ogg into per-instrument stems via Demucs."""
    if sloppak_path.is_dir():
        _split_in_dir(sloppak_path, model, progress_cb, base_frac, span_frac)
        return

    # Zip form: unpack, split, re-zip atomically.
    with tempfile.TemporaryDirectory(prefix="s2p_split_zip_") as td:
        work = Path(td) / "sloppak"
        work.mkdir()
        with zipfile.ZipFile(str(sloppak_path), "r") as zf:
            zf.extractall(work)

        _split_in_dir(work, model, progress_cb, base_frac, span_frac * 0.9)

        _progress(progress_cb, base_frac + span_frac * 0.95, "packing",
                  "Repacking sloppak")
        tmp_out = sloppak_path.with_suffix(sloppak_path.suffix + ".tmp")
        with zipfile.ZipFile(str(tmp_out), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in work.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(work).as_posix())
        tmp_out.replace(sloppak_path)
