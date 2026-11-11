"""Fetches and updates the external binaries NooNoo drives: yt-dlp, ffmpeg, ffprobe and deno.

Everything lives in ~/Library/Application Support/NooNoo/bin so the project folder stays code-only.
Downloads go through curl (always present on macOS and uses the system trust store), and each
one must pass verify.py before it can replace anything. A failed check keeps the existing copy.
"""
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from . import verify

DATA_DIR = Path.home() / "Library" / "Application Support" / "NooNoo"
BIN_DIR = DATA_DIR / "bin"

_ARM = platform.machine() == "arm64"
_FFMPEG_BASE = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/%s/release/" % ("arm64" if _ARM else "amd64")

SOURCES = {
    # The "onedir" zip build: the single-file build unpacks itself (and gets re-scanned by macOS)
    # on every launch, ~13s per call; the unpacked one starts in ~0.3s.
    "yt-dlp": (None, "yt-dlp_macos/"),  # resolved to one release, so the file and its signature match
    "deno": ("https://github.com/denoland/deno/releases/latest/download/deno-%s-apple-darwin.zip"
             % ("aarch64" if _ARM else "x86_64"), "deno"),
    "ffmpeg": (_FFMPEG_BASE + "ffmpeg.zip", "ffmpeg"),
    # ffprobe has no "latest" redirect; it is resolved next to wherever ffmpeg.zip redirects to.
    "ffprobe": (None, "ffprobe"),
}

YTDLP_LATEST = "https://github.com/yt-dlp/yt-dlp/releases/latest"
YTDLP_RELEASE = "https://github.com/yt-dlp/yt-dlp/releases/download/%s/"
YTDLP_ARCHIVE = "yt-dlp_macos.zip"
YTDLP_SUMS = "SHA2-256SUMS"

VERSION_ARGS = {
    "yt-dlp": ["--version"],
    "deno": ["--version"],
    "ffmpeg": ["-hide_banner", "-version"],
    "ffprobe": ["-hide_banner", "-version"],
}


def path(name):
    return BIN_DIR / name


def secure_data_dir():
    """Download history, settings and the tools themselves are for this account only."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)


def missing():
    return [n for n in SOURCES if not os.access(path(n), os.X_OK)]


def env():
    """Environment for child processes: our bin dir first so yt-dlp finds deno/ffmpeg itself."""
    e = dict(os.environ)
    e["PATH"] = str(BIN_DIR) + os.pathsep + e.get("PATH", "")
    e["PYTHONUNBUFFERED"] = "1"
    return e


def version(name):
    p = path(name)
    if not p.exists():
        return None
    try:
        out = subprocess.run([str(p)] + VERSION_ARGS[name], capture_output=True, text=True, timeout=30).stdout
        first = out.strip().splitlines()[0] if out.strip() else ""
        if name.startswith("ff"):  # "ffmpeg version 9.0.2 Copyright ..." -> "9.0.2"
            parts = first.split()
            return (parts[2] if len(parts) > 2 else first).split("-http")[0]
        if name == "deno":  # "deno 2.9.8 (stable, ...)" -> "2.9.8"
            return first.split()[1] if len(first.split()) > 1 else first
        return first
    except Exception:
        return None


def status():
    return {n: version(n) for n in SOURCES}


# HTTPS only, including every redirect.
_CURL = ["/usr/bin/curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "--retry", "3"]


def _curl(url, dest):
    subprocess.run(_CURL + ["-o", str(dest), url], check=True)


def _resolve(url):
    # a 1-byte GET follows redirects more reliably than HEAD on some mirrors
    out = subprocess.run(_CURL + ["-r", "0-0", "-o", "/dev/null", "-w", "%{url_effective}", url],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _ytdlp_release():
    """Base URL of the latest yt-dlp release, by its tag."""
    effective = _resolve(YTDLP_LATEST)
    tag = effective.rstrip("/").rsplit("/tag/", 1)[-1]
    if "/tag/" not in effective or not re.fullmatch(r"[0-9A-Za-z._-]+", tag):
        raise verify.VerificationError("couldn't find the latest yt-dlp release")
    return YTDLP_RELEASE % tag


def _fetch_ytdlp(dl, tmp, base=None):
    """Download yt-dlp and prove it against the checksums yt-dlp signed."""
    base = base or _ytdlp_release()
    sums, sig = Path(tmp) / "sums", Path(tmp) / "sums.sig"
    _curl(base + YTDLP_SUMS, sums)
    _curl(base + YTDLP_SUMS + ".sig", sig)
    verify.verify_pgp(sums.read_bytes(), sig.read_bytes())  # before downloading anything large
    _curl(base + YTDLP_ARCHIVE, dl)
    verify.verify_ytdlp(dl, YTDLP_ARCHIVE, sums.read_bytes(), sig.read_bytes())


def _install(name, log, base=None):
    url, member = SOURCES[name]
    if name == "ffprobe":
        url = _resolve(SOURCES["ffmpeg"][0]).rsplit("/", 1)[0] + "/ffprobe.zip"
    log("Downloading %s ..." % name)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="noonoo-") as tmp:
        dl = Path(tmp) / "download"
        if name == "yt-dlp":
            _fetch_ytdlp(dl, tmp, base)
        else:
            _curl(url, dl)
        if member and member.endswith("/"):  # whole-directory build behind a small wrapper script
            # A new folder each time, so downloads already running keep their copy; old ones go at next launch.
            dist = BIN_DIR / ("%s-dist-%d" % (name, time.time() * 1000))
            staged = Path(tmp) / "dist"
            subprocess.run(["unzip", "-q", str(dl), "-d", str(staged)], check=True)  # keeps exec bits
            shutil.move(str(staged), str(dist))
            wrapper = Path(tmp) / name
            wrapper.write_text('#!/bin/sh\nexec "$(dirname "$0")/%s/%s" "$@"\n' % (dist.name, member.rstrip("/")))
            dl = wrapper
        elif member:
            with zipfile.ZipFile(dl) as z:
                entry = next(i for i in z.namelist() if i.rstrip("/").split("/")[-1] == member)
                with z.open(entry) as src, open(Path(tmp) / member, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            dl = Path(tmp) / member
            verify.verify_team(dl, name)
        dl.chmod(0o755)
        shutil.move(str(dl), str(path(name).with_name(name + ".new")))
        os.replace(path(name).with_name(name + ".new"), path(name))   # never a moment with no tool
    # first launch of a new binary is slow while macOS scans it; do it now rather than on the first search
    log("Installed %s %s" % (name, version(name) or ""))


def ensure(log=print, names=None):
    """Install any missing tools. Returns the list of names that failed."""
    failed = []
    for n in names or missing():
        try:
            _install(n, log)
        except Exception as e:  # keep going; the UI reports what is missing
            log("Failed to install %s: %s" % (n, e))
            failed.append(n)
    return failed


def prune_old_builds():
    """Remove yt-dlp folders the wrapper no longer points at. Only safe before anything runs yt-dlp."""
    try:
        current = re.search(r'/([^/"]+)/[^/"]+" "\$@"', path("yt-dlp").read_text()).group(1)
    except (OSError, AttributeError):
        return
    for d in BIN_DIR.glob("yt-dlp-dist*"):
        if d.is_dir() and d.name != current:
            shutil.rmtree(d, ignore_errors=True)


def update_ytdlp(log=print):
    """Install the newest yt-dlp release if this isn't it. Returns the version installed, or None."""
    base = _ytdlp_release()
    latest = base.rstrip("/").rsplit("/", 1)[-1]
    if version("yt-dlp") == latest:
        return None
    _install("yt-dlp", log, base)
    return latest


def update(log=print):
    """Reinstall every tool from its latest release (extractors change constantly)."""
    before = status()
    failed = ensure(log, list(SOURCES))
    after = status()
    for n in SOURCES:
        if n in failed:
            continue
        log("%s: %s" % (n, "up to date (%s)" % after[n] if before[n] == after[n] else "%s -> %s" % (before[n], after[n])))
    return failed


if __name__ == "__main__":
    bad = ensure()
    print(status())
    raise SystemExit(1 if bad else 0)
