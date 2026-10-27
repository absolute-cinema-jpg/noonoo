"""NooNoo updates: finds a newer release on GitHub, downloads and verifies it, and swaps it in.

The check is one anonymous request for a small JSON file. Nothing about the user is sent.
Installing needs NooNoo.app (the app tells us where it lives via NOONOO_APP); in browser mode
we can only point at the release page.
"""
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import settings as settings_mod
from .tools import DATA_DIR

# Public repo whose GitHub releases NooNoo updates from (release.sh publishes there).
FEED_REPO = "absolute-cinema-jpg/noonoo"
FEED_URL = os.environ.get("NOONOO_UPDATE_FEED") or "https://api.github.com/repos/%s/releases/latest" % FEED_REPO
BUNDLE_ID = "local.noonoo.NooNoo"
STAGE_DIR = DATA_DIR / "update"
STATE_FILE = DATA_DIR / "update.json"          # last check time + version the user chose to skip
RESUME_FILE = DATA_DIR / "resume-after-update.json"
CHECK_EVERY = 6 * 3600


def app_path():
    p = os.environ.get("NOONOO_APP", "")
    return p if p.endswith(".app") and os.path.isdir(p) else ""


def _version_of(app):
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        return info.get("CFBundleShortVersionString", ""), info.get("CFBundleIdentifier", "")
    except (OSError, ValueError):
        return "", ""


def current_version():
    app = app_path()
    if app:
        return _version_of(app)[0]
    try:  # running from source: use the version the next build will have
        with open(os.path.join(os.path.dirname(__file__), "..", "app", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString", "")
    except (OSError, ValueError):
        return ""


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def mac_flavour():
    """Which build suits this Mac - by hardware, so an Intel copy running under Rosetta moves to native."""
    try:
        arm = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True).stdout.strip() == "1"
    except OSError:
        arm = os.uname().machine == "arm64"
    return "Apple-Silicon" if arm else "Intel"


def _open(url, proxy, timeout=20):
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={"User-Agent": "NooNoo/%s" % (current_version() or "dev"),
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def _notes(markdown):
    """Release notes as plain text for the XP dialog: markdown symbols and tables trimmed."""
    lines = []
    for line in (markdown or "").splitlines():
        if re.match(r"^\s*\|?\s*-{3,}", line):
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"\*\*(.+?)\*\*|`(.+?)`", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"^(\s*)[-*] ", r"\1• ", line)
        lines.append(line.strip("| ").replace(" | ", " · ").rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"status": "idle", "current": current_version(), "latest": "", "notes": "", "page": "",
                      "size": None, "progress": 0, "error": "", "can_install": bool(app_path()), "skipped": ""}
        self._asset = None
        self._saved = self._load()
        self.state["skipped"] = self._saved.get("skipped", "")

    # ---- persisted bits --------------------------------------------------
    def _load(self):
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, **kw):
        self._saved.update(kw)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._saved))
        except OSError:
            pass

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    # ---- checking ----------------------------------------------------------
    def start_background_checks(self):
        def loop():
            time.sleep(8)   # let the window open first
            while True:
                s = settings_mod.load()
                due = time.time() - self._saved.get("checked", 0) > CHECK_EVERY
                if s["auto_update_check"] and due and self.state["status"] in ("idle", "uptodate", "error"):
                    self.check()
                time.sleep(900)
        threading.Thread(target=loop, daemon=True).start()

    def check(self):
        if self.state["status"] in ("checking", "downloading", "installing"):
            return self.snapshot()
        self._set(status="checking", error="")
        try:
            with _open(FEED_URL, settings_mod.load()["proxy"]) as r:
                rel = json.loads(r.read().decode())
        except Exception as e:  # offline, rate limited, feed not published yet...
            self._set(status="error", error="Couldn't reach the update server (%s)" % getattr(e, "reason", e))
            return self.snapshot()
        self._save(checked=time.time())
        latest = (rel.get("tag_name") or "").lstrip("v")
        flavour = mac_flavour()
        asset = next((a for a in rel.get("assets", []) if a.get("name", "").endswith("-%s.zip" % flavour)), None)
        newer = parse_version(latest) > parse_version(self.state["current"])
        self._asset = asset
        self._set(latest=latest, notes=_notes(rel.get("body")), page=rel.get("html_url", ""),
                  size=(asset or {}).get("size"),
                  status="available" if newer and asset else "uptodate",
                  error="" if asset or not newer else "Version %s has no %s build" % (latest, flavour.replace("-", " ")))
        return self.snapshot()

    def skip(self, version):
        self._save(skipped=version)
        self._set(skipped=version)

    # ---- downloading ---------------------------------------------------------
    def download(self):
        if self.state["status"] != "available" or not self._asset:
            return self.snapshot()
        self._set(status="downloading", progress=0, error="")
        threading.Thread(target=self._download, daemon=True).start()
        return self.snapshot()

    def _download(self):
        a = self._asset
        try:
            shutil.rmtree(STAGE_DIR, ignore_errors=True)
            STAGE_DIR.mkdir(parents=True)
            zpath = STAGE_DIR / a["name"]
            digest = hashlib.sha256()
            with _open(a["browser_download_url"], settings_mod.load()["proxy"], timeout=60) as r, open(zpath, "wb") as f:
                total, done = a.get("size") or 0, 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        self._set(progress=round(100 * done / total, 1))
            want = (a.get("digest") or "").replace("sha256:", "")
            if want and want != digest.hexdigest():
                raise RuntimeError("the download was damaged (checksum mismatch) - try again")
            subprocess.run(["ditto", "-x", "-k", str(zpath), str(STAGE_DIR / "new")], check=True, capture_output=True)
            app = STAGE_DIR / "new" / "NooNoo.app"
            version, bundle = _version_of(str(app))
            if bundle != BUNDLE_ID or parse_version(version) != parse_version(self.state["latest"]):
                raise RuntimeError("the download isn't the expected NooNoo %s" % self.state["latest"])
            subprocess.run(["codesign", "--verify", "--deep", str(app)], check=True, capture_output=True)
            zpath.unlink()
            self._set(status="ready", progress=100)
        except Exception as e:
            self._set(status="available", error="Update failed: %s" % e)

    # ---- installing ------------------------------------------------------------
    def install(self, engine):
        """Hand over to a helper that waits for NooNoo to quit, swaps the app and opens the new one."""
        dest, new = app_path(), STAGE_DIR / "new" / "NooNoo.app"
        if self.state["status"] != "ready" or not dest or not new.is_dir():
            return {"ok": False, "error": "Nothing to install"}
        parent = os.path.dirname(dest)
        if not (os.access(parent, os.W_OK) and os.access(dest, os.W_OK)):
            subprocess.Popen(["open", "-R", str(new)])
            return {"ok": False, "error": "NooNoo can't replace itself in %s (no permission). The new version is "
                                         "selected in Finder: drag it into that folder to finish." % parent}
        engine.remember_for_update(RESUME_FILE)
        script = STAGE_DIR / "install.sh"
        script.write_text(INSTALL_SCRIPT)
        log = open(STAGE_DIR / "install.log", "w")
        subprocess.Popen(["/bin/sh", str(script), str(os.getppid()), str(os.getpid()), str(new), dest],
                         stdout=log, stderr=log, start_new_session=True)
        self._set(status="installing")
        # ask NooNoo.app to quit; the helper takes over once the app and this engine have exited
        print("NOONOO_QUIT", flush=True)
        return {"ok": True}


# Runs detached: wait for the app and engine to exit, swap bundles (rolling back on failure), reopen.
INSTALL_SCRIPT = r"""#!/bin/sh
app_pid=$1; engine_pid=$2; new=$3; dest=$4
for i in $(seq 1 200); do
  kill -0 "$app_pid" 2>/dev/null || kill -0 "$engine_pid" 2>/dev/null || break
  sleep 0.3
done
old="$dest.previous"
rm -rf "$old"
if mv "$dest" "$old" && mv "$new" "$dest"; then
  rm -rf "$old"
  echo "installed $(date)"
else
  echo "swap failed, restoring the previous version"
  [ -d "$dest" ] || mv "$old" "$dest"
fi
xattr -dr com.apple.quarantine "$dest" 2>/dev/null
open "$dest"
"""
