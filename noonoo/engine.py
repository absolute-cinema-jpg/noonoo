"""Download queue and metadata lookups, both driven by the yt-dlp binary.

Each download is one yt-dlp process. We ask yt-dlp to print machine-readable lines
(progress, post-processing, final path) with unique prefixes and parse those.
Editing formats (DNxHR, ProRes) and H.264 compatibility are produced afterwards by an
ffmpeg pass, so the download itself can always take the highest resolution on offer.
"""
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import struct
import time
import unicodedata
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from . import settings as settings_mod
from . import spotify
from . import tools

PROG, PP, INFO, DONE = "@@NN_PROG ", "@@NN_PP ", "@@NN_INFO ", "@@NN_DONE "
JOBS_FILE = tools.DATA_DIR / "jobs.json"
TEMP_DIR = tools.DATA_DIR / "tmp"
ARCHIVE_DIR = tools.DATA_DIR / "archives"

ACTIVE = ("starting", "downloading", "processing", "converting")
FINISHED = ("done", "error", "cancelled", "skipped")
AUDIO_SITES = {"soundcloud", "bandcamp", "mixcloud", "audiomack", "spotify", "applepodcasts",
               "podbeanpodcast", "hearthisat", "jamendo", "audius", "deezer"}
URL_RE = re.compile(r"^(https?://|www\.)|^[\w-]+(\.[\w-]+)+/\S*$", re.I)
LOGIN_RE = re.compile(r"log ?in|logged|sign in|authenticat|cookies|private video|members.only|"
                      r"confirm your age|age.restrict|not a bot|rate.limit", re.I)

_PCM = ["-c:a", "pcm_s24le", "-ar", "48000"]          # what Avid and most NLEs expect
_DNXHR = lambda p: ["-c:v", "dnxhd", "-profile:v", p, "-pix_fmt", "yuv422p"]

# key -> label, container yt-dlp merges into, and (for converted formats) the ffmpeg pass.
# DNxHD is Avid's HD codec: fixed 1920x1080, a fixed bitrate per frame rate. DNxHR is the same family
# without those limits, for keeping 4K / vertical / odd sizes at their own resolution.
VIDEO_FORMATS = {
    "h264":     {"label": "MP4 · H.264", "merge": "mp4", "ext": "mp4", "edit": False},
    "dnxhd_lb": {"label": "DNxHD LB (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "dnxhd": 0},
    "dnxhd_sq": {"label": "DNxHD SQ (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "dnxhd": 1},
    "dnxhd_hq": {"label": "DNxHD HQ (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "dnxhd": 2},
    "dnxhr_lb": {"label": "DNxHR LB (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _DNXHR("dnxhr_lb")},
    "dnxhr_sq": {"label": "DNxHR SQ (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _DNXHR("dnxhr_sq")},
    "dnxhr_hq": {"label": "DNxHR HQ (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _DNXHR("dnxhr_hq")},
}
# DNxHD 1080p bitrates (Mb/s) per frame rate for LB / SQ / HQ - the only combinations Avid defines
DNXHD_RATES = {"24000/1001": (36, 115, 175), "24/1": (36, 115, 175), "25/1": (36, 120, 185),
               "30000/1001": (45, 145, 220), "30/1": (45, 145, 220), "50/1": (75, 240, 365),
               "60000/1001": (90, 290, 440), "60/1": (90, 290, 440)}


def _rate_value(r):
    n, _, d = str(r).partition("/")
    try:
        return float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def nearest_dnxhd_rate(rate):
    """DNxHD only exists at broadcast frame rates; pick the closest one to the source."""
    v = _rate_value(rate) or 30.0
    return min(DNXHD_RATES, key=lambda r: abs(_rate_value(r) - v))


AUDIO_FORMATS = {"mp3": "MP3", "m4a": "M4A", "opus": "Opus", "flac": "FLAC", "wav": "WAV",
                 "wav_avid": "WAV 48k/24-bit", "best": "Original"}


def is_url(text):
    return bool(URL_RE.search(text.strip())) or spotify.is_spotify(text)


def _num(v):
    return v if isinstance(v, (int, float)) else None


# --------------------------------------------------------------------------- browser logins

def _host(url):
    try:
        return (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""


def login_allowed(s, url):
    """Only share the browser login with sites the user ticked in Options > Accounts."""
    if not s["cookies_browser"]:
        return False
    host = _host(url)
    domains = [d.strip().lower().lstrip(".") for d in s["cookies_custom"].split(",") if d.strip()]
    for key in s["cookies_sites"].split(","):
        domains += settings_mod.COOKIE_SITES.get(key.strip(), ("", []))[1]
    return any(host == d or host.endswith("." + d) for d in domains)


def login_args(s):
    spec = s["cookies_browser"] + (":" + s["cookies_profile"].strip() if s["cookies_profile"].strip() else "")
    return ["--cookies-from-browser", spec]


def login_label(s):
    return s["cookies_browser"].capitalize() + (" (%s)" % s["cookies_profile"] if s["cookies_profile"].strip() else "")


def _login_hint(s, url):
    host = _host(url).replace("www.", "") or "this site"
    if not s["cookies_browser"]:
        return "%s wants you signed in. Tools > Options > Accounts can lend NooNoo your browser login." % host
    if not login_allowed(s, url):
        return "%s wants you signed in. Allow your login for it in Tools > Options > Accounts." % host
    return ""


def _net_args(s):
    return ["--proxy", s["proxy"]] if s["proxy"] else []


# --------------------------------------------------------------------------- search / probe

class Searcher:
    def __init__(self):
        self._procs = {}
        self._lock = threading.Lock()

    def _once(self, sid, args, s, with_login):
        cmd = [str(tools.path("yt-dlp")), "-J", "--flat-playlist", "--no-warnings", "--no-colors"]
        cmd += _net_args(s) + (login_args(s) if with_login else []) + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=tools.env(), start_new_session=True)
        with self._lock:
            self._procs.setdefault(sid, []).append(proc)
        out, err = proc.communicate()
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data, ""
        if proc.returncode < 0:
            raise RuntimeError("Search stopped")
        msg = [l for l in err.splitlines() if "ERROR" in l] or err.strip().splitlines()[-1:] or ["yt-dlp failed"]
        return None, msg[-1]

    def _run(self, sid, args, s, url, used):
        allowed = login_allowed(s, url)
        first = allowed and s["cookies_mode"] == "always"
        data, err = self._once(sid, args, s, first)
        if data is None and allowed and not first and LOGIN_RE.search(err):
            data, err = self._once(sid, args, s, True)
            first = True
        if data is None:
            raise RuntimeError(_friendly_error(err, s, url))
        if first:
            used.add(_host(url).replace("www.", ""))
        return data

    def search(self, sid, query, source="youtube"):
        s = settings_mod.load()
        query = query.strip()
        used = set()
        try:
            urls = [u for u in re.split(r"\s+", query) if u] if query and all(is_url(u) for u in query.split()) else []
            if urls:
                urls = [u if u.lower().startswith(("http", "spotify:")) else "https://" + u for u in urls]
                limit = ["--playlist-end", str(s["playlist_items_limit"])]
                items, errors = [], []
                with ThreadPoolExecutor(max_workers=min(6, len(urls))) as pool:
                    futs = [pool.submit(self._spotify, sid, u, s, used, errors) if spotify.is_spotify(u)
                            else pool.submit(lambda u=u: _flatten(self._run(sid, limit + ["--", u], s, u, used)))
                            for u in urls]
                    for u, f in zip(urls, futs):
                        try:
                            items += f.result()
                        except Exception as e:
                            errors.append("%s: %s" % (u, e))
                if not items and errors:
                    raise RuntimeError("\n".join(errors))
                return {"items": items, "errors": errors, "login": sorted(used), "login_browser": login_label(s)}
            prefix = {"youtube": "ytsearch", "soundcloud": "scsearch", "youtube-new": "ytsearchdate"}.get(source, "ytsearch")
            site = "https://soundcloud.com" if prefix == "scsearch" else "https://www.youtube.com"
            data = self._run(sid, ["--", "%s%d:%s" % (prefix, s["search_results"], query)], s, site, used)
            return {"items": _flatten(data), "errors": [], "login": sorted(used), "login_browser": login_label(s)}
        finally:
            with self._lock:
                self._procs.pop(sid, None)

    def _spotify(self, sid, url, s, used, errors):
        """Spotify can't be downloaded directly (DRM), so each song becomes its YouTube upload."""
        info = spotify.lookup(url, s["proxy"])
        tracks = info["tracks"][:max(1, s["playlist_items_limit"])]
        if not tracks:
            raise RuntimeError("No songs found")

        def match(t):
            data = self._run(sid, ["--", "ytsearch5:" + spotify.query(t)], s, "https://www.youtube.com", used)
            e = spotify.best_match(t, data.get("entries") or [])
            if not e:
                return None
            it = _item(e, None)
            album = info["name"] if info["type"] == "album" else ""
            extra = t if t["cover"] or not t["id"] else spotify.track_details(t["id"], s["proxy"])
            cover = extra["cover"] or info["cover"] or it["thumbnail"]
            it.update(title=t["title"], uploader=t["artists"], site="Spotify", kind="audio",
                      duration=t["duration"] or it["duration"], thumbnail=cover, thumb_small=cover,
                      playlist=info["name"] if info["type"] != "track" else "", views=None, likes=None,
                      date=extra["date"].replace("-", ""), album=album,
                      tags={"title": t["title"], "artist": t["artists"], "album": album,
                            "date": extra["date"], "cover": extra["cover"] or info["cover"]})
            return it

        out = []
        with ThreadPoolExecutor(max_workers=min(6, len(tracks))) as pool:
            for t, f in zip(tracks, [pool.submit(match, t) for t in tracks]):
                try:
                    it = f.result()
                except RuntimeError as e:
                    if str(e) == "Search stopped":
                        raise
                    it = None
                if it:
                    out.append(it)
                else:
                    errors.append("No YouTube match for %s" % spotify.query(t))
        return out

    def details(self, sid, urls, kind="video"):
        """Upload date, album, resolution and size for search results - the quick listing YouTube gives a
        keyword search leaves these out. One extra request per video; like downloads, it only uses your
        browser login if the site refuses and Options > Accounts allows it."""
        s = settings_mod.load()
        allowed = urls and login_allowed(s, urls[0])
        out, err = self._details_once(sid, urls, kind, s, allowed and s["cookies_mode"] == "always")
        if not out and allowed and s["cookies_mode"] != "always" and LOGIN_RE.search(err):
            out, err = self._details_once(sid, urls, kind, s, True)
        return out

    def _details_once(self, sid, urls, kind, s, with_login):
        # size of what a download would actually fetch: best audio for songs, picture + sound for video
        fmt = "ba/b" if kind == "audio" else "bv*[format_id!*=-sr]+ba/b[format_id!*=-sr]/bv*+ba/b"
        cmd = [str(tools.path("yt-dlp")), "--skip-download", "--no-warnings", "--no-colors", "--ignore-errors", "-f", fmt,
               "-O", "%(.{original_url,upload_date,height,filesize_approx,filesize,view_count,album})j"]
        cmd += _net_args(s) + (login_args(s) if with_login else []) + ["--"] + urls
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=tools.env(), start_new_session=True)
        with self._lock:
            self._procs.setdefault(sid, []).append(proc)
        out = {}
        try:
            stdout, stderr = proc.communicate()
            for line in stdout.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                size = _num(d.get("filesize")) or _num(d.get("filesize_approx"))
                out[d.get("original_url")] = {"date": d.get("upload_date") or "", "size": size,
                                              "quality": ("%dp" % d["height"]) if _num(d.get("height")) and kind != "audio" else "",
                                              "views": _num(d.get("view_count")), "album": d.get("album") or ""}
        finally:
            with self._lock:
                if proc in self._procs.get(sid, []):
                    self._procs[sid].remove(proc)
        return out, stderr

    def cancel(self, sid=None):
        with self._lock:
            procs = [p for k, ps in self._procs.items() if sid in (None, k) for p in ps]
        for p in procs:
            _kill(p, signal.SIGTERM)


def _flatten(data, parent=None):
    if data.get("_type") in ("playlist", "multi_video") and data.get("entries") is not None:
        out = []
        for e in data["entries"]:
            if e:
                out += _flatten(e, data)
        return out
    return [_item(data, parent)]


def _item(e, parent):
    site = (e.get("extractor_key") or e.get("ie_key") or (parent or {}).get("extractor_key") or "").replace(":", " ")
    url = e.get("webpage_url") or e.get("original_url") or e.get("url") or ""
    if url and not url.startswith("http") and site.lower().startswith("youtube"):
        url = "https://www.youtube.com/watch?v=" + url
    thumbs = [t for t in e.get("thumbnails") or [] if t.get("url")]
    thumb = e.get("thumbnail") or (thumbs[-1]["url"] if thumbs else "")
    sized = sorted((t for t in thumbs if t.get("width")), key=lambda t: t["width"])
    small = next((t["url"] for t in sized if t["width"] >= 160), sized[-1]["url"] if sized else thumb)
    size = _num(e.get("filesize")) or _num(e.get("filesize_approx"))
    if not size and e.get("requested_formats"):
        size = sum(_num(f.get("filesize")) or _num(f.get("filesize_approx")) or 0 for f in e["requested_formats"]) or None
    height = _num(e.get("height"))
    audio_only = e.get("vcodec") == "none" or site.lower() in AUDIO_SITES
    return {
        "url": url,
        "title": e.get("title") or e.get("fulltitle") or url,
        "site": site or "Generic",
        "uploader": e.get("uploader") or e.get("channel") or e.get("creator") or (parent or {}).get("uploader") or "",
        "duration": _num(e.get("duration")),
        "views": _num(e.get("view_count")),
        "likes": _num(e.get("like_count")),
        "size": size,
        "ext": e.get("ext") or "",
        "quality": ("%dp" % height) if height else "",
        "thumbnail": thumb or "",
        "thumb_small": small or "",
        "kind": "audio" if audio_only else "video",
        "playlist": (parent or {}).get("title") or "",
        "album": e.get("album") or "",
        "date": e.get("upload_date") or "",
    }


def _kill(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# --------------------------------------------------------------------------- folders

def _safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(s)).strip(" .")
    return s[:80] or "Unknown"


def target_dir(s, kind, item, base=None):
    """Where a new download goes: chosen folder plus the optional auto-subfolder."""
    d = os.path.expanduser(base or s["current_folder"] or s["output_dir"])
    part = {
        "site": item.get("site"),
        "uploader": item.get("uploader"),
        "playlist": item.get("playlist") or "Singles",
        "date": time.strftime("%Y-%m-%d"),
    }.get(s["organize"])
    if part:
        d = os.path.join(d, _safe_name(part))
    return d


# --------------------------------------------------------------------------- downloads

class Job:
    FIELDS = ("id", "url", "title", "site", "uploader", "thumbnail", "duration", "kind", "quality",
              "status", "progress", "downloaded", "total", "speed", "eta", "stage", "filepath",
              "error", "added", "finished", "part", "parts", "vformat", "aformat", "folder",
              "raw_file", "used_login", "playlist", "tags", "fps")

    def __init__(self, **kw):
        self.id = uuid.uuid4().hex[:10]
        self.url = ""
        self.title = ""
        self.site = ""
        self.uploader = ""
        self.thumbnail = ""
        self.duration = None
        self.kind = "video"
        self.quality = ""
        self.status = "queued"
        self.progress = 0.0
        self.downloaded = 0
        self.total = None
        self.speed = None
        self.eta = None
        self.stage = ""
        self.filepath = ""
        self.error = ""
        self.added = time.time()
        self.finished = None
        self.part = 0
        self.parts = 1
        self.vformat = ""
        self.aformat = ""
        self.folder = ""
        self.raw_file = ""        # downloaded file still waiting for its ffmpeg conversion
        self.used_login = ""      # browser whose login was used, shown to the user
        self.playlist = ""
        self.tags = None          # {"title", "artist", "album"} from Spotify, written over YouTube's
        self.fps = "source"       # frame rate to conform converted video to
        for k, v in kw.items():
            if k in self.FIELDS:
                setattr(self, k, v)
        self.proc = None
        self.log = deque(maxlen=300)
        self.tmpfiles = set()
        self.force = False
        self._intent = None  # "pause" | "cancel" while we are stopping the process

    def to_dict(self, with_log=False):
        d = {k: getattr(self, k) for k in self.FIELDS}
        if with_log:
            d["log"] = list(self.log)
        return d

    def converts(self):
        return self.kind == "video" and "ext" in VIDEO_FORMATS.get(self.vformat, {})

    def fps_rate(self):
        """Target frame rate as an ffmpeg rational, or None to keep the source's."""
        if not self.fps or self.fps == "source":
            return None
        return {"23.976": "24000/1001", "29.97": "30000/1001", "59.94": "60000/1001"}.get(self.fps, self.fps)


class Engine:
    def __init__(self):
        self.jobs = {}
        self.order = []
        self.lock = threading.RLock()
        self.version = 0          # bumps on every change so the UI can skip redundant renders
        self._dirty = False
        self._load()
        threading.Thread(target=self._saver, daemon=True).start()

    # ---- persistence -----------------------------------------------------
    def _load(self):
        try:
            for d in json.loads(JOBS_FILE.read_text()):
                j = Job(**d)
                j.id = d["id"]
                if j.status in ACTIVE or j.status == "queued":
                    j.status, j.speed, j.eta = "paused", None, None
                self.jobs[j.id] = j
                self.order.append(j.id)
        except (OSError, ValueError, KeyError):
            pass
        self._resume_after_update()
        if TEMP_DIR.is_dir():  # workspaces of jobs that no longer exist
            for d in TEMP_DIR.iterdir():
                if d.name not in self.jobs:
                    shutil.rmtree(d, ignore_errors=True)

    def _saver(self):
        while True:
            time.sleep(2)
            if not self._dirty:
                continue
            self._dirty = False
            with self.lock:
                data = [self.jobs[i].to_dict() for i in self.order]
            try:
                tools.DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = JOBS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                tmp.replace(JOBS_FILE)
            except OSError:
                pass

    def _changed(self):
        self.version += 1
        self._dirty = True

    # ---- public API --------------------------------------------------------
    def snapshot(self):
        with self.lock:
            jobs = [self.jobs[i].to_dict() for i in self.order]
        active = [j for j in jobs if j["status"] in ACTIVE]
        return {
            "version": self.version,
            "jobs": jobs,
            "active": len(active),
            "queued": sum(1 for j in jobs if j["status"] == "queued"),
            "speed": sum(j["speed"] or 0 for j in active if j["status"] == "downloading"),
        }

    def job(self, jid, with_log=False):
        with self.lock:
            j = self.jobs.get(jid)
            return j.to_dict(with_log) if j else None

    def known_files(self):
        with self.lock:
            return {p for j in self.jobs.values() for p in (j.filepath, j.raw_file) if p}

    def add(self, items, kind, quality="", vformat="", aformat="", folder="", fps=""):
        s = settings_mod.load()
        fps = fps or s["edit_fps"]
        vformat = vformat if vformat in VIDEO_FORMATS else s["video_format"]
        aformat = aformat if aformat in AUDIO_FORMATS else s["audio_format"]
        added = []
        with self.lock:
            for it in items:
                if not it.get("url"):
                    continue
                j = Job(url=it["url"], title=it.get("title") or it["url"], site=it.get("site", ""),
                        uploader=it.get("uploader", ""), thumbnail=it.get("thumbnail", ""),
                        duration=it.get("duration"), kind=kind, quality=quality, vformat=vformat,
                        aformat=aformat, playlist=it.get("playlist", ""), tags=it.get("tags"), fps=fps,
                        folder=target_dir(s, kind, it, folder))
                self.jobs[j.id] = j
                self.order.append(j.id)
                added.append(j.id)
            self._changed()
        self._pump()
        return added

    def action(self, jid, act):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return False
            if act == "pause":
                if j.status == "queued":
                    j.status = "paused"
                elif j.status in ACTIVE:
                    self._stop(j, "pause")
            elif act == "resume":
                if j.status in ("paused", "error", "cancelled"):
                    self._reset(j)
            elif act == "retry":
                if j.status not in ACTIVE:
                    self._reset(j)
                    j.force, j.raw_file = True, ""
            elif act == "cancel":
                if j.status in ACTIVE:
                    self._stop(j, "cancel")
                elif j.status in ("queued", "paused"):
                    j.status = "cancelled"
                    self._cleanup_tmp(j)
            elif act == "remove":
                if j.status in ACTIVE:
                    self._stop(j, "cancel")
                else:
                    self._cleanup_tmp(j)
                self.order.remove(jid)
                del self.jobs[jid]
            elif act == "up" or act == "down":
                i = self.order.index(jid)
                k = i - 1 if act == "up" else i + 1
                if 0 <= k < len(self.order):
                    self.order[i], self.order[k] = self.order[k], self.order[i]
            self._changed()
        self._pump()
        return True

    def bulk(self, act):
        with self.lock:
            ids = list(self.order)
        for jid in ids:
            with self.lock:
                j = self.jobs.get(jid)
                status = j.status if j else None
            if act == "clear" and status in FINISHED:
                self.action(jid, "remove")
            elif act == "pause_all" and status in ACTIVE + ("queued",):
                self.action(jid, "pause")
            elif act == "resume_all" and status == "paused":
                self.action(jid, "resume")

    def remember_for_update(self, path):
        """Note which downloads are running or queued, so they carry on once the updated app opens."""
        with self.lock:
            ids = [i for i in self.order if self.jobs[i].status in ACTIVE + ("queued",)]
        try:
            path.write_text(json.dumps(ids))
        except OSError:
            pass

    def _resume_after_update(self):
        from .updater import RESUME_FILE
        try:
            ids = json.loads(RESUME_FILE.read_text())
            RESUME_FILE.unlink()
        except (OSError, ValueError):
            return
        for jid in ids:
            j = self.jobs.get(jid)
            if j and j.status == "paused":
                self._reset(j)
        threading.Timer(2, self._pump).start()

    def shutdown(self):
        with self.lock:
            for j in self.jobs.values():
                if j.proc and j.status in ACTIVE:
                    j._intent = "pause"
                    _kill(j.proc, signal.SIGINT)
                    j.status = "paused"
            self._changed()
        self._dirty = False
        data = [self.jobs[i].to_dict() for i in self.order]
        try:
            JOBS_FILE.write_text(json.dumps(data))
        except OSError:
            pass

    # ---- internals ---------------------------------------------------------
    def _reset(self, j):
        j.status, j.error, j.stage, j.speed, j.eta = "queued", "", "", None, None
        j.finished, j.part, j._cur, j._base = None, 0, None, 0

    def _stop(self, j, intent):
        j._intent = intent
        j.stage = "Stopping..."
        if j.proc:
            _kill(j.proc, signal.SIGINT)

    def _pump(self):
        s = settings_mod.load()
        with self.lock:
            running = sum(1 for j in self.jobs.values() if j.status in ACTIVE)
            for jid in self.order:
                if running >= max(1, s["max_concurrent"]):
                    break
                j = self.jobs[jid]
                if j.status == "queued":
                    j.status, j.stage, j._intent = "starting", "Connecting...", None
                    running += 1
                    threading.Thread(target=self._run, args=(j, s), daemon=True).start()
            self._changed()

    def _run(self, j, s):
        if j.converts() and j.raw_file and os.path.exists(j.raw_file):
            result, err = "ok", ""      # downloaded earlier; only the conversion is left
            j.filepath = j.raw_file
        else:
            allowed = login_allowed(s, j.url)
            with_login = allowed and s["cookies_mode"] == "always"
            result, err = self._download(j, s, with_login)
            if result == "error" and allowed and not with_login and LOGIN_RE.search(err):
                j.log.append("== %s needs a login: retrying once with your %s login" % (_host(j.url), login_label(s)))
                with self.lock:
                    j.status, j.stage, j.progress, j.part, j._cur, j._base = "starting", "Signing in...", 0, 0, None, 0
                with_login = True
                result, err = self._download(j, s, True)
            if with_login and result == "ok":
                j.used_login = login_label(s)
        if result == "ok" and j.filepath and j.filepath.startswith(str(TEMP_DIR)) and j.tags:
            result, err = self._retag(j, s, j.filepath)
        if result == "ok" and j.kind == "audio" and j.fps_rate() and j.filepath.lower().endswith(".wav") \
                and j.filepath.startswith(str(TEMP_DIR)):
            result, err = self._stamp_bwf(j, j.filepath)
        if result == "ok" and j.filepath and j.filepath.startswith(str(TEMP_DIR)):
            if j.converts():
                j.raw_file = j.filepath
                result, err = self._convert(j, s)
            else:
                result, err = self._deliver(j, j.filepath)
        with self.lock:
            j.proc, j.speed, j.eta = None, None, None
            if result == "paused":
                j.status, j.stage = "paused", ""
            elif result == "cancelled":
                j.status, j.stage = "cancelled", ""
                self._cleanup_tmp(j)
            elif result == "ok":
                self._cleanup_tmp(j)
                j.status = "done" if j.filepath else "skipped"
                j.stage = "" if j.filepath else "Already downloaded to this folder in this format"
                j.progress = 100.0
                if j.filepath:
                    try:
                        j.total = j.downloaded = os.path.getsize(j.filepath)
                    except OSError:
                        pass
            else:
                j.status, j.stage, j.error = "error", "", _friendly_error(err, s, j.url)
            j.finished = time.time()
            j._intent = None
            self._changed()
        if j.status == "done" and s["notify_done"]:
            notify("Download complete", j.title)
        self._pump()

    def _download(self, j, s, with_login):
        """One yt-dlp run. Returns (ok|paused|cancelled|error, last error line)."""
        try:
            args = build_args(j, s, with_login)
            j.log.append("$ " + " ".join(shlex.quote(a) for a in args))
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    bufsize=1, env=tools.env(), start_new_session=True, errors="replace")
        except Exception as e:
            return "error", "Could not start yt-dlp: %s" % e
        j.proc = proc
        errors = []
        t = threading.Thread(target=self._read_err, args=(j, proc, errors), daemon=True)
        t.start()
        for line in proc.stdout:
            self._parse(j, line.rstrip("\n"))
        proc.wait()
        t.join(timeout=5)
        if j._intent:
            return ("paused" if j._intent == "pause" else "cancelled"), ""
        if proc.returncode == 0:
            return "ok", ""
        return "error", errors[-1] if errors else "yt-dlp exited with code %s" % proc.returncode

    def _convert(self, j, s):
        """ffmpeg pass to H.264 / DNxHR / ProRes. Returns (ok|paused|cancelled|error, message)."""
        spec = VIDEO_FORMATS[j.vformat]
        src = j.raw_file
        try:
            probe = json.loads(subprocess.run(
                [str(tools.path("ffprobe")), "-v", "error", "-show_streams", "-show_format", "-of", "json", src],
                capture_output=True, text=True, timeout=60).stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return "error", "Couldn't read the downloaded file: %s" % e
        streams = probe.get("streams", [])
        video = [x for x in streams if x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")]
        pics = [x for x in streams if x.get("codec_type") == "video" and x.get("disposition", {}).get("attached_pic")]
        audio = [x for x in streams if x.get("codec_type") == "audio"]
        main = video[0] if video else {}
        if not video or (j.vformat == "h264" and main.get("codec_name") == "h264" and src.endswith(".mp4")
                         and not j.fps_rate() and all(a.get("codec_name") in ("aac", "mp3") for a in audio)):
            return self._deliver(j, src)  # audio-only source, or already plays everywhere

        dst = os.path.join(os.path.dirname(src), "converting." + spec["ext"])
        duration = float(probe.get("format", {}).get("duration") or j.duration or 0)
        fps = main.get("avg_frame_rate") or main.get("r_frame_rate") or "30/1"
        if fps in ("0/0", "0/1"):
            fps = main.get("r_frame_rate") or "30/1"

        cmd = [str(tools.path("ffmpeg")), "-hide_banner", "-nostdin", "-y", "-i", src,
               "-map", "0:%d" % main["index"], "-map", "0:a:0?", "-map_metadata", "0",
               "-filter:v:0", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]  # even sizes; leaves cover art alone
        if "dnxhd" in spec:
            rate = nearest_dnxhd_rate(j.fps_rate() or fps)
            mbps = DNXHD_RATES[rate][spec["dnxhd"]]
            fit = ("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1")   # letterbox / pillarbox anything that isn't 16:9
            i = cmd.index("-filter:v:0")
            cmd[i + 1] = fit
            cmd += ["-c:v", "dnxhd", "-profile:v", "dnxhd", "-b:v", "%dM" % mbps, "-pix_fmt", "yuv422p",
                    "-r", rate, "-fps_mode", "cfr", "-map_chapters", "-1", "-dn", "-movflags", "+write_colr"] + _PCM
        elif spec.get("edit"):
            cmd += spec["v"] + ["-r", j.fps_rate() or fps, "-fps_mode", "cfr", "-map_chapters", "-1", "-dn"] + _PCM
            if spec["ext"] == "mov":
                cmd += ["-movflags", "+write_colr"]
        else:  # H.264 on the Mac's media engine; bitrate scaled to picture size
            w, h = main.get("width") or 1920, main.get("height") or 1080
            if j.fps_rate():
                cmd += ["-r", j.fps_rate(), "-fps_mode", "cfr"]
            try:
                n, _, d = (j.fps_rate() or fps).partition("/")
                rate = float(n) / float(d or 1)
            except (ValueError, ZeroDivisionError):
                rate = 30.0
            kbps = max(2500, int(w * h * min(rate, 60) * 0.12 / 1000))
            cmd += ["-c:v:0", "h264_videotoolbox", "-b:v:0", "%dk" % kbps, "-profile:v:0", "high",
                    "-pix_fmt:v:0", "yuv420p", "-tag:v:0", "avc1", "-movflags", "+faststart"]
            ok_audio = audio and audio[0].get("codec_name") in ("aac", "mp3")
            cmd += ["-c:a", "copy"] if ok_audio else ["-c:a", "aac", "-b:a", "256k"]
            if pics:  # keep the cover art the download embedded
                cmd += ["-map", "0:%d" % pics[0]["index"], "-c:v:1", "copy", "-disposition:v:1", "attached_pic"]
        cmd += ["-progress", "pipe:1", "-nostats", dst]

        label = spec["label"].split(" (")[0]
        if "dnxhd" in spec:
            label = "DNxHD %d" % DNXHD_RATES[nearest_dnxhd_rate(j.fps_rate() or fps)][spec["dnxhd"]]
        with self.lock:
            j.status, j.stage, j.progress, j.speed, j.eta = "converting", "Converting to " + label, 0, None, None
            self._changed()
        rc, err = self._ffmpeg(j, cmd, duration, label)
        if rc != 0 and not j._intent and not spec.get("edit"):
            # no hardware encoder (old Intel Mac, odd size): software x264 instead
            j.log.append("== hardware H.264 failed, retrying with x264")
            i = cmd.index("h264_videotoolbox")
            cmd[i:i + 3] = ["libx264", "-crf", "18", "-preset", "fast"]
            rc, err = self._ffmpeg(j, cmd, duration, label)
        if j._intent or rc != 0:
            try:
                os.remove(dst)
            except OSError:
                pass
            if j._intent:
                return ("paused" if j._intent == "pause" else "cancelled"), ""
            return "error", "Conversion to %s failed: %s" % (label, err)
        try:
            final = _place(dst, j.folder, os.path.splitext(os.path.basename(src))[0] + "." + spec["ext"])
        except OSError as e:
            return "error", "Couldn't save into %s: %s" % (j.folder, e)
        if s["keep_original"]:
            self._deliver(j, src)
        j.filepath, j.raw_file = final, ""
        return "ok", ""

    def _retag(self, j, s, src):
        """Spotify songs: write Spotify's title/artist/album/year and album cover, and name the file after
        the song. Done here rather than through yt-dlp, which reads a one-word value such as "Believer"
        as the name of a field and writes "NA"."""
        tags = j.tags or {}
        work, ext = os.path.dirname(src), os.path.splitext(src)[1].lower()
        dst = os.path.join(work, "tagged" + ext)
        cover = _fetch_cover(tags.get("cover"), work, s["proxy"]) if j.kind == "audio" and s["embed_thumbnail"] else None
        cmd = [str(tools.path("ffmpeg")), "-hide_banner", "-nostdin", "-y", "-i", src]
        if j.kind != "audio":
            cmd += ["-map", "0", "-map_metadata", "0"]
        elif cover and ext in (".mp3", ".m4a", ".flac"):
            cmd += ["-i", cover, "-map", "0:a", "-map", "1:0", "-map_metadata", "0", "-disposition:v:0", "attached_pic",
                    "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        else:
            cmd += ["-map", "0:a", "-map_metadata", "0"]
            if cover and ext in (".opus", ".ogg"):   # Ogg keeps artwork as a FLAC picture block tag
                cmd += ["-metadata", "METADATA_BLOCK_PICTURE=" + _flac_picture(cover)]
        for field in ("title", "artist", "album", "date"):
            if tags.get(field):
                cmd += ["-metadata", "%s=%s" % (field, tags[field])]
                if ext in (".opus", ".ogg"):   # Ogg keeps its comments on the audio stream
                    cmd += ["-metadata:s:a:0", "%s=%s" % (field, tags[field])]
        if ext == ".mp3":
            cmd += ["-id3v2_version", "3"]
        cmd += ["-c", "copy", dst]
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if out.returncode != 0:
            j.log.append(out.stderr.strip()[-500:])
            return "error", "Couldn't write the song's tags: %s" % (out.stderr.strip().splitlines() or ["ffmpeg failed"])[-1]
        named = os.path.join(work, song_filename(s, tags, os.path.basename(src)) + ext)
        os.replace(dst, named)
        if named != src:
            os.remove(src)
        j.filepath = named
        return "ok", ""

    def _stamp_bwf(self, j, src):
        """Make a WAV a Broadcast WAV that carries the chosen frame rate. Audio has no frame rate of its own,
        so editors like Avid read it from these fields (bext "sSPEED" and iXML TIMECODE_RATE) or guess."""
        rate = j.fps_rate()
        speed = "%07.3f-ND" % _rate_value(rate)                  # 23.976 -> "023.976-ND"
        tmp = os.path.join(os.path.dirname(src), "bwf.wav")
        cmd = [str(tools.path("ffmpeg")), "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0:a", "-map_metadata", "0",
               "-c", "copy", "-write_bext", "1", "-metadata", "originator=NooNoo", "-metadata", "time_reference=0",
               "-metadata", "description=sSPEED=%s\r\n" % speed, tmp]
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if out.returncode != 0:
            j.log.append(out.stderr.strip()[-500:])
            return "error", "Couldn't write the frame rate into the WAV file"
        rational = rate if "/" in rate else rate + "/1"
        ixml = ('<?xml version="1.0" encoding="UTF-8"?>\n<BWFXML><IXML_VERSION>1.61</IXML_VERSION>'
                "<PROJECT>NooNoo</PROJECT><SPEED><MASTER_SPEED>{r}</MASTER_SPEED><CURRENT_SPEED>{r}</CURRENT_SPEED>"
                "<TIMECODE_RATE>{r}</TIMECODE_RATE><TIMECODE_FLAG>NDF</TIMECODE_FLAG>"
                "<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>0</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>"
                "<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>0</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>"
                "</SPEED></BWFXML>\n").format(r=rational).encode()
        if len(ixml) % 2:
            ixml += b" "                                          # RIFF chunks are word-aligned
        with open(tmp, "r+b") as f:                              # append an iXML chunk, then fix the RIFF size
            f.seek(0, 2)
            f.write(b"iXML" + struct.pack("<I", len(ixml)) + ixml)
            size = f.tell()
            f.seek(4)
            f.write(struct.pack("<I", size - 8))
        os.replace(tmp, src)
        return "ok", ""

    def _deliver(self, j, src):
        """Move a finished file out of the job's workspace into its destination folder."""
        try:
            final = _place(src, j.folder, os.path.basename(src))
        except OSError as e:
            return "error", "Couldn't save into %s: %s" % (j.folder, e)
        j.filepath, j.raw_file = final, ""
        return "ok", ""

    def _ffmpeg(self, j, cmd, duration, label):
        j.log.append("$ " + " ".join(shlex.quote(a) for a in cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    bufsize=1, start_new_session=True, errors="replace")
        except OSError as e:
            return 1, str(e)
        j.proc = proc
        tail = deque(maxlen=8)
        t = threading.Thread(target=lambda: [tail.append(l.rstrip()) for l in proc.stderr], daemon=True)
        t.start()
        pos, speed = 0.0, None
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.isdigit():
                pos = int(v) / 1e6
            elif k == "speed":
                try:
                    speed = float(v.rstrip("x"))
                except ValueError:
                    speed = None
            elif k == "progress":
                with self.lock:
                    if duration:
                        j.progress = round(min(99.9, 100 * pos / duration), 1)
                    if speed:
                        j.eta = max(0, (duration - pos) / speed) if duration else None
                        j.stage = "Converting · %.1f× realtime" % speed
                    self._changed()
        proc.wait()
        t.join(timeout=5)
        for l in tail:
            j.log.append(l)
        errs = [l for l in tail if l and not l.startswith("[out#")]
        return proc.returncode, (errs[-1] if errs else "ffmpeg exited with code %s" % proc.returncode)

    def _read_err(self, j, proc, errors):
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("@@NN_"):  # in quiet mode some progress lines arrive on stderr
                self._parse(j, line)
                continue
            j.log.append(line)
            if line.startswith("ERROR"):
                errors.append(line)

    def _parse(self, j, line):
        try:
            if line.startswith(PROG):
                p = json.loads(line[len(PROG):])
                with self.lock:
                    if j._intent:
                        return
                    for key in ("tmpfilename", "filename"):
                        if p.get(key):
                            j.tmpfiles.add(p[key])
                    fname = p.get("filename") or ""
                    if fname and fname != getattr(j, "_cur", None):
                        if getattr(j, "_cur", None) is not None:
                            j.part = min(j.part + 1, j.parts - 1)
                            j._base = j.downloaded
                        j._cur = fname
                    done = _num(p.get("downloaded_bytes")) or 0
                    total = _num(p.get("total_bytes")) or _num(p.get("total_bytes_estimate"))
                    frac = (done / total) if total else 0
                    if p.get("status") == "finished":
                        frac = 1
                    j.status = "downloading"
                    j.downloaded = getattr(j, "_base", 0) + done
                    if total and j.parts == 1:
                        j.total = total
                    elif total and not j.total:
                        j.total = total
                    j.progress = round(min(99.9, 100 * (j.part + frac) / max(1, j.parts)), 1)
                    j.speed = _num(p.get("speed"))
                    j.eta = _num(p.get("eta"))
                    j.stage = ("Stream %d of %d" % (j.part + 1, j.parts)) if j.parts > 1 else ""
                    self._changed()
            elif line.startswith(PP):
                p = json.loads(line[len(PP):])
                with self.lock:
                    if j._intent:
                        return
                    j.status, j.speed, j.eta = "processing", None, None
                    j.stage = _PP_LABELS.get(p.get("postprocessor"), p.get("postprocessor") or "Processing")
                    self._changed()
            elif line.startswith(INFO):
                info = json.loads(line[len(INFO):])
                with self.lock:
                    j.title = info.get("title") or j.title
                    j.site = info.get("extractor_key") or j.site
                    j.thumbnail = j.thumbnail or info.get("thumbnail") or ""
                    j.duration = j.duration or info.get("duration")
                    j.uploader = j.uploader or info.get("uploader") or ""
                    fid = info.get("format_id") or ""
                    j.parts = fid.count("+") + 1 if fid else 1
                    j.total = _num(info.get("filesize_approx")) or _num(info.get("filesize")) or j.total
                    if info.get("height") and j.kind == "video":
                        j.quality = "%sp" % info["height"]
                    self._changed()
            elif line.startswith(DONE):
                with self.lock:
                    j.filepath = json.loads(line[len(DONE):])
                    self._changed()
            elif line.strip():
                j.log.append(line)
        except ValueError:
            j.log.append(line)

    def _cleanup_tmp(self, j):
        shutil.rmtree(TEMP_DIR / j.id, ignore_errors=True)
        for f in list(j.tmpfiles):
            for cand in (f, f + ".part", f + ".ytdl", f + ".part-Frag1"):
                try:
                    os.remove(cand)
                except OSError:
                    pass
        j.tmpfiles.clear()


_PP_LABELS = {
    "Merger": "Merging audio + video",
    "ExtractAudio": "Converting audio",
    "FFmpegExtractAudio": "Converting audio",
    "EmbedThumbnail": "Embedding artwork",
    "FFmpegMetadata": "Writing tags",
    "Metadata": "Writing tags",
    "FFmpegEmbedSubtitle": "Embedding subtitles",
    "FFmpegVideoRemuxer": "Remuxing",
    "FFmpegVideoConvertor": "Converting video",
    "SponsorBlock": "Checking SponsorBlock",
    "ModifyChapters": "Cutting sponsor segments",
    "MoveFiles": "Moving to folder",
    "FFmpegFixupM3u8": "Fixing container",
    "FFmpegFixupM4a": "Fixing container",
}

_ERROR_HINTS = [
    (r"Unsupported URL", "This site isn't supported by yt-dlp (or the link isn't a media page)."),
    (r"Requested format is not available", "That quality isn't available - try 'Best' in Options."),
    (r"HTTP Error 403", "Server refused (403). Try Tools > Update Download Engine."),
    (r"No video could be found", "This post has no video in it."),
    (r"Unsupported video frame rate|frame rate", "MXF needs a standard frame rate - pick one under Options > Video, or use .mov."),
]


def _friendly_error(line, s=None, url=""):
    msg = re.sub(r"^ERROR:\s*", "", line).strip()
    msg = re.sub(r"\s*Use --cookies.*$", "", msg)  # replaced by our own hint below
    msg = re.sub(r"\s*See\s+https://github.com/yt-dlp/yt-dlp/wiki/FAQ\S*.*$", "", msg)
    if s is not None and LOGIN_RE.search(msg):
        hint = _login_hint(s, url)
        return "%s  %s" % (msg, hint) if hint else msg
    for pat, hint in _ERROR_HINTS:
        if re.search(pat, msg, re.I):
            return "%s  %s" % (msg, hint)
    return msg


NOTIFY_VIA_APP = False   # set by `python -m noonoo --serve`: NooNoo.app shows them with its own icon


def notify(title, text):
    if NOTIFY_VIA_APP:
        print("NOONOO_NOTIFY " + json.dumps({"title": title, "text": text[:200]}), flush=True)
        return
    script = 'display notification %s with title %s' % (json.dumps(text[:200]), json.dumps(title))
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


_FILENAME_SWAPS = str.maketrans({"/": "⧸", "\\": "⧹", ":": "：", "*": "＊", "?": "？", '"': "＂",
                                  "<": "＜", ">": "＞", "|": "｜"})


def song_filename(s, tags, fallback):
    """Just the Spotify song title - never the YouTube upload's title, artist or id."""
    name = (tags.get("title") or "").strip()
    if not name:
        return os.path.splitext(fallback)[0]
    if s["restrict_filenames"]:
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        name = re.sub(r"[^\w.-]+", "_", name).strip("_.")
    else:
        name = re.sub(r"[\x00-\x1f]", "", name.translate(_FILENAME_SWAPS)).strip().lstrip(".")
    return name.encode()[:200].decode("utf-8", "ignore") or os.path.splitext(fallback)[0]


def _fetch_cover(url, folder, proxy=""):
    if not url:
        return None
    path = os.path.join(folder, "cover.jpg")
    try:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        with urllib.request.build_opener(*handlers).open(url, timeout=20) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
        return path
    except OSError:
        return None


def _flac_picture(path):
    """Base64 FLAC PICTURE block (front cover), the way Ogg/Opus files carry artwork."""
    data = open(path, "rb").read()
    mime = b"image/jpeg"
    block = (struct.pack(">II", 3, len(mime)) + mime + struct.pack(">I", 0) +
             struct.pack(">IIIII", 0, 0, 0, 0, len(data)) + data)
    return base64.b64encode(block).decode()


_place_lock = threading.Lock()


def _place(src, folder, name):
    """Move src into folder as name - or 'name (2)' etc. - claiming the name first so two jobs
    finishing at the same moment can't pick the same one."""
    os.makedirs(folder, exist_ok=True)
    with _place_lock:
        final = _free_name(os.path.join(folder, name))
        open(final, "xb").close()          # placeholder holds the name while we move
    try:
        shutil.move(src, final)
    except OSError:
        try:
            os.remove(final)
        except OSError:
            pass
        raise
    return final


def _free_name(path):
    """path, or 'name (2).ext' etc. if something's already there."""
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = "%s (%d)%s" % (stem, n, ext)
        n += 1
    return path


def _archive(j):
    """Download archive per folder + format, so a new copy elsewhere (or as DNxHR) isn't 'already downloaded'."""
    key = hashlib.sha1(("%s|%s|%s" % (j.kind, j.vformat if j.kind == "video" else j.aformat, j.folder)).encode()).hexdigest()[:12]
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    return str(ARCHIVE_DIR / (key + ".txt"))


def output_template(s):
    t = s["filename_template"] or settings_mod.DEFAULTS["filename_template"]
    if s["filename_id"] and "%(id)" not in t:
        t = re.sub(r"\.%\(ext\)s$", " [%(id)s].%(ext)s", t)
    return t


def build_args(j, s, with_login=False):
    r = str(s["retries"])
    a = [str(tools.path("yt-dlp")),
         "--newline", "--progress", "--no-simulate", "--no-colors", "--no-playlist",
         "--progress-template", "download:" + PROG + "%(progress.{status,downloaded_bytes,total_bytes,"
                                                     "total_bytes_estimate,speed,eta,filename,tmpfilename})j",
         "--progress-template", "postprocess:" + PP + "%(progress.{status,postprocessor})j",
         "--print", "video:" + INFO + "%(.{title,extractor_key,thumbnail,duration,uploader,format_id,"
                                      "filesize_approx,filesize,height})j",
         "--print", "after_move:" + DONE + "%(filepath)j",
         "--ffmpeg-location", str(tools.BIN_DIR),
         "--retries", r, "--fragment-retries", r,
         "-N", str(max(1, s["fragments"])),
         # everything lands in the job's workspace first, then moves to its folder under a free name -
         # so two different videos with the same title never get mistaken for each other
         "-P", str(TEMP_DIR / j.id / "out"), "-P", "temp:" + str(TEMP_DIR / j.id),
         "-o", output_template(s)]
    a += _net_args(s)
    if with_login:
        a += login_args(s)
    if s["rate_limit"]:
        a += ["-r", s["rate_limit"]]
    if s["restrict_filenames"]:
        a.append("--restrict-filenames")
    if s["skip_existing"] and not j.force:
        a += ["--download-archive", _archive(j)]

    thumb_ok = True
    if j.kind == "audio":
        fmt = j.aformat or s["audio_format"]
        a += ["-f", "ba/b", "-x", "--audio-quality", s["audio_quality"] or "0"]
        if fmt == "wav_avid":
            a += ["--audio-format", "wav", "--postprocessor-args", "ExtractAudio:-c:a pcm_s24le -ar 48000"]
        elif fmt != "best":
            a += ["--audio-format", fmt]
        thumb_ok = fmt not in ("wav", "wav_avid")
    else:
        spec = VIDEO_FORMATS.get(j.vformat or s["video_format"], VIDEO_FORMATS["h264"])
        q = (j.quality or s["video_quality"]).rstrip("p")
        if "dnxhd" in spec and (not q.isdigit() or int(q) > 1080):
            q = "1080"   # DNxHD is 1080p; a 4K download would only be scaled down
        # Resolution always ranks first: "best" means the biggest picture. Among equal sizes prefer
        # H.264/AAC/SDR when we're converting anyway (or targeting H.264) - fastest and safest to decode.
        sort = ["res:" + q if q.isdigit() else "res"]
        if j.converts() and j.fps_rate():  # a native rate at or under the target means fewer dropped frames
            n, _, d = j.fps_rate().partition("/")
            sort.append("fps:%d" % -(-float(n) // float(d or 1)))
        if "ext" in spec:
            sort += ["hdr:sdr", "vcodec:h264", "acodec:aac"]
        # skip YouTube's AI-upscaled "-sr" streams unless they're all there is
        a += ["-f", "bv*[format_id!*=-sr]+ba/b[format_id!*=-sr]/bv*+ba/b",
              "--merge-output-format", spec["merge"], "-S", ",".join(sort)]
        edit = spec.get("edit")
        thumb_ok = not edit
        if s["subtitles"] and not edit:
            a += ["--write-subs", "--sub-langs", s["sub_langs"] or "en.*", "--embed-subs"]
        if s["embed_chapters"] and not edit:
            a.append("--embed-chapters")
    if s["embed_thumbnail"] and thumb_ok and not j.tags:   # Spotify songs get their album cover in _retag
        a += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    if s["embed_metadata"]:
        a.append("--embed-metadata")
    if s["sponsorblock"]:
        a += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
    if s["extra_args"].strip():
        a += shlex.split(s["extra_args"])
    a += ["--", j.url]
    return a
