"""Fetches and updates the external binaries NooNoo drives: yt-dlp, ffmpeg, ffprobe and deno.

Everything lives in ~/Library/Application Support/NooNoo/bin so the project folder stays code-only.
Downloads go through curl (always present on macOS and uses the system trust store).
"""
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

DATA_DIR = Path.home() / "Library" / "Application Support" / "NooNoo"
BIN_DIR = DATA_DIR / "bin"

_ARM = platform.machine() == "arm64"
_FFMPEG_BASE = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/%s/release/" % ("arm64" if _ARM else "amd64")

SOURCES = {
    # The "onedir" zip build: the single-file build unpacks itself (and gets re-scanned by macOS)
    # on every launch, ~13s per call; the unpacked one starts in ~0.3s.
    "yt-dlp": ("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos.zip", "yt-dlp_macos/"),
    "deno": ("https://github.com/denoland/deno/releases/latest/download/deno-%s-apple-darwin.zip"
             % ("aarch64" if _ARM else "x86_64"), "deno"),
    "ffmpeg": (_FFMPEG_BASE + "ffmpeg.zip", "ffmpeg"),
    # ffprobe has no "latest" redirect; it is resolved next to wherever ffmpeg.zip redirects to.
    "ffprobe": (None, "ffprobe"),
}

VERSION_ARGS = {
    "yt-dlp": ["--version"],
    "deno": ["--version"],
    "ffmpeg": ["-hide_banner", "-version"],
    "ffprobe": ["-hide_banner", "-version"],
}


def path(name):
    return BIN_DIR / name


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


def _curl(url, dest):
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(dest), url], check=True)


def _resolve(url):
    # a 1-byte GET follows redirects more reliably than HEAD on some mirrors
    out = subprocess.run(["curl", "-fsSL", "--retry", "3", "-r", "0-0", "-o", "/dev/null", "-w", "%{url_effective}", url],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _install(name, log):
    url, member = SOURCES[name]
    if name == "ffprobe":
        url = _resolve(SOURCES["ffmpeg"][0]).rsplit("/", 1)[0] + "/ffprobe.zip"
    log("Downloading %s ..." % name)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="noonoo-") as tmp:
        dl = Path(tmp) / "download"
        _curl(url, dl)
        if member and member.endswith("/"):  # whole-directory build behind a small wrapper script
            dist = BIN_DIR / (name + "-dist")
            staged = Path(tmp) / "dist"
            subprocess.run(["unzip", "-q", str(dl), "-d", str(staged)], check=True)  # keeps exec bits
            shutil.rmtree(dist, ignore_errors=True)
            shutil.move(str(staged), str(dist))
            wrapper = Path(tmp) / name
            wrapper.write_text('#!/bin/sh\nexec "$(dirname "$0")/%s-dist/%s" "$@"\n' % (name, member.rstrip("/")))
            dl = wrapper
        elif member:
            with zipfile.ZipFile(dl) as z:
                entry = next(i for i in z.namelist() if i.rstrip("/").split("/")[-1] == member)
                with z.open(entry) as src, open(Path(tmp) / member, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            dl = Path(tmp) / member
        dl.chmod(0o755)
        path(name).unlink(missing_ok=True)
        shutil.move(str(dl), str(path(name)))
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
