"""Download queue and metadata lookups, both driven by the yt-dlp binary.

Each download is one yt-dlp process. We ask yt-dlp to print machine-readable lines
(progress, post-processing, final path) with unique prefixes and parse those.
Editing formats (DNxHR, ProRes) and H.264 compatibility are produced afterwards by an
ffmpeg pass, so the download itself can always take the highest resolution on offer.
"""
import base64
import hashlib
import json
import math
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
# "trim": part of a video has downloaded and waits for the user to set its exact first and last frames
CLIP_PAD = 2.0   # seconds downloaded either side of a part, so there's room to fine-trim
AUDIO_SITES = {"soundcloud", "bandcamp", "mixcloud", "audiomack", "spotify", "applepodcasts",
               "podbeanpodcast", "hearthisat", "jamendo", "audius", "deezer"}
URL_RE = re.compile(r"^(https?://|www\.)|^[\w-]+(\.[\w-]+)+/\S*$", re.I)
# YouTube turns some requests from a connection away for a while ("not a bot" checks, 403s on the video
# data, rate limits). The same request often works seconds later, so NooNoo asks again before giving up.
TRANSIENT_RE = re.compile(r"not a bot|HTTP Error 403|HTTP Error 429|Too Many Requests|rate.limit|"
                          r"unable to download video data|timed out|Connection reset", re.I)
RETRY_WAITS = (4, 12)        # seconds before each extra try at a download
SEARCH_RETRY_WAITS = (2, 5)  # ...and at a link lookup, which someone is watching
LOGIN_RE = re.compile(r"log ?in|logged|sign in|authenticat|cookies|private video|members.only|"
                      r"confirm your age|age.restrict|not a bot|rate.limit", re.I)
# The site turned a logged-out request away. YouTube can list a video logged out and then refuse its
# picture with a 403 that never mentions signing in, so these count as "refused" too. Facebook shows
# logged-out visitors a page with no video data in it, which yt-dlp reports as "Cannot parse data".
# Time-outs don't count.
REFUSED_RE = re.compile(LOGIN_RE.pattern + r"|HTTP Error 403|HTTP Error 429|Too Many Requests|Cannot parse data", re.I)

_PCM = ["-c:a", "pcm_s24le", "-ar", "48000"]          # what Avid and most NLEs expect
_DNXHR = lambda p: ["-c:v", "dnxhd", "-profile:v", p, "-pix_fmt", "yuv422p"]

# key -> label, container yt-dlp merges into, and (for converted formats) the ffmpeg pass.
# DNxHD is Avid's HD codec: fixed 1920x1080, a fixed bitrate per frame rate. DNxHR is the same family
# without those limits, for keeping 4K / vertical / odd sizes at their own resolution.
_PRORES = lambda p: ["-c:v", "prores_ks", "-profile:v", p, "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
VIDEO_FORMATS = {
    "h264":     {"label": "MP4 · H.264", "merge": "mp4", "ext": "mp4", "edit": False},
    "hevc":     {"label": "MP4 · H.265 (HEVC)", "merge": "mp4", "ext": "mp4", "edit": False, "hevc": True},
    "prores_proxy": {"label": "ProRes 422 Proxy (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _PRORES("0")},
    "prores_lt":    {"label": "ProRes 422 LT (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _PRORES("1")},
    "prores_422":   {"label": "ProRes 422 (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _PRORES("2")},
    "prores_hq":    {"label": "ProRes 422 HQ (.mov)", "merge": "mkv", "ext": "mov", "edit": True, "v": _PRORES("3")},
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
# how a cut piece of audio is encoded (yt-dlp does this for whole downloads): extension, ffmpeg codec
AUDIO_ENCODERS = {"mp3": ("mp3", ["-c:a", "libmp3lame"]), "m4a": ("m4a", ["-c:a", "aac"]),
                  "opus": ("opus", ["-c:a", "libopus"]), "flac": ("flac", ["-c:a", "flac"]),
                  "wav": ("wav", ["-c:a", "pcm_s16le"]), "wav_avid": ("wav", _PCM)}


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
        self._stopped = set()
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

    def _run(self, sid, args, s, url, used, empty_is_refusal=False):
        """empty_is_refusal: for searches. YouTube answers some searches logged out with "Confirm your
        age" and no results (Small Faces - Itchycoo Park), which yt-dlp reports as an empty list."""
        allowed = login_allowed(s, url)
        first = allowed and s["cookies_mode"] == "always"
        data, err = self._once(sid, args, s, first)
        if data is not None and empty_is_refusal and not data.get("entries"):
            if not (allowed and not first):
                return data
            data, err = None, "No results logged out (YouTube may want you to confirm your age)"
        if data is None and allowed and not first and REFUSED_RE.search(err):
            first = True
            data, err = self._once(sid, args, s, True)
        for wait in SEARCH_RETRY_WAITS:
            if data is not None or not TRANSIENT_RE.search(err):
                break
            deadline = time.time() + wait
            while time.time() < deadline:
                if sid in self._stopped:
                    raise RuntimeError("Search stopped")
                time.sleep(0.2)
            data, err = self._once(sid, args, s, first)
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
            data = self._run(sid, ["--", "%s%d:%s" % (prefix, s["search_results"], query)], s, site, used,
                             empty_is_refusal=prefix != "scsearch")
            return {"items": _flatten(data), "errors": [], "login": sorted(used), "login_browser": login_label(s)}
        finally:
            with self._lock:
                self._procs.pop(sid, None)
                self._stopped.discard(sid)

    def _spotify(self, sid, url, s, used, errors):
        """Spotify can't be downloaded directly (DRM), so each song becomes its YouTube upload."""
        info = spotify.lookup(url, s["proxy"])
        tracks = info["tracks"][:max(1, s["playlist_items_limit"])]
        if not tracks:
            raise RuntimeError("No songs found")

        def match(t):
            e = None
            # Logged out first, in two wordings (YouTube age-checks some searches but not others), then the login.
            for q, login_ok in ((spotify.query(t), False), (spotify.query(t, title_first=True), False),
                                (spotify.query(t), True)):
                data = self._run(sid, ["--", "ytsearch5:" + q], s, "https://www.youtube.com", used, empty_is_refusal=login_ok)
                e = spotify.best_match(t, data.get("entries") or [])
                if e:
                    break
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
                    errors.append("%s: couldn't search YouTube (%s)" % (spotify.query(t), e))
                    continue
                if it:
                    out.append(it)
                else:
                    errors.append("%s: not found on YouTube" % spotify.query(t))
        return out

    def details(self, sid, urls, kind="video"):
        """Upload date, album, resolution and size for search results - the quick listing YouTube gives a
        keyword search leaves these out. One extra request per video; like downloads, it only uses your
        browser login if the site refuses and Options > Accounts allows it."""
        s = settings_mod.load()
        allowed = urls and login_allowed(s, urls[0])
        out, err = self._details_once(sid, urls, kind, s, allowed and s["cookies_mode"] == "always")
        if not out and allowed and s["cookies_mode"] != "always" and REFUSED_RE.search(err):
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
            self._stopped.update(self._procs if sid is None else [sid])
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
    d = os.path.expanduser(base or s["output_dir"])
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
              "raw_file", "used_login", "playlist", "tags", "fps", "filename",
              "clip_start", "clip_end", "fine_trim", "trim_start", "trim_end",
              "clip_offset", "clip_fps", "clip_frames", "clip_len", "source", "edit_id", "height", "target_mb")

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
        self.filename = ""        # name the user chose, without an extension; "" = the usual pattern
        # Part of a video: the rough range typed before downloading (seconds into the video), then the
        # exact one picked in the trim window (seconds into the downloaded part: for video, frame n is at
        # n / clip_fps). clip_offset is where in the video the part starts (its first frame, for video);
        # clip_fps/frames/len describe the part, and clip_fps is "" for sound only.
        self.clip_start = self.clip_end = None
        self.fine_trim = False
        self.trim_start = self.trim_end = None
        self.clip_offset = 0.0
        self.clip_fps = ""
        self.clip_frames = 0
        self.clip_len = None
        # An export from the Edit tab: made from a file already on the Mac (never moved or changed), with
        # edit_id naming its measurements in the edit library. height (short side) and target_mb (a file
        # size to fit into) apply to any converted video; 0 = keep the size / no target.
        self.source = ""
        self.edit_id = ""
        self.height = 0
        self.target_mb = 0
        for k, v in kw.items():
            if k in self.FIELDS:
                setattr(self, k, v)
        self.proc = None
        self.log = deque(maxlen=300)
        self.tmpfiles = set()
        self.force = False
        self._full = False   # the site wouldn't send just a part, so the whole video was downloaded
        self._intent = None  # "pause" | "cancel" while we are stopping the process

    def to_dict(self, with_log=False):
        d = {k: getattr(self, k) for k in self.FIELDS}
        if with_log:
            d["log"] = list(self.log)
        return d

    def converts(self):
        return self.kind == "video" and "ext" in VIDEO_FORMATS.get(self.vformat, {})

    def is_clip(self):
        return self.clip_start is not None and self.clip_end is not None

    def clip_bounds(self):
        """The stretch actually downloaded for a part: the typed range plus room either side."""
        a = max(0.0, self.clip_start - CLIP_PAD)
        b = self.clip_end + CLIP_PAD
        if self.duration:
            b = min(b, float(self.duration) + 1)
        return a, b

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
            "trim": sum(1 for j in jobs if j["status"] == "trim"),
            "speed": sum(j["speed"] or 0 for j in active if j["status"] == "downloading"),
        }

    def job(self, jid, with_log=False):
        with self.lock:
            j = self.jobs.get(jid)
            return j.to_dict(with_log) if j else None

    def known_files(self):
        with self.lock:
            return {p for j in self.jobs.values() for p in (j.filepath, j.raw_file) if p}

    def add(self, items, kind, quality="", vformat="", aformat="", folder="", fps="", clip=None):
        s = settings_mod.load()
        clip = _clean_clip(clip)
        fps = fps or s["edit_fps"]
        vformat = vformat if vformat in VIDEO_FORMATS else s["video_format"]
        aformat = aformat if aformat in AUDIO_FORMATS else s["audio_format"]
        added = []
        with self.lock:
            for it in items:
                if not it.get("url"):
                    continue
                name = custom_filename(s, it.get("filename"))
                j = Job(url=it["url"], title=name or it.get("title") or it["url"], filename=name, site=it.get("site", ""),
                        uploader=it.get("uploader", ""), thumbnail=it.get("thumbnail", ""),
                        duration=it.get("duration"), kind=kind, quality=quality, vformat=vformat,
                        aformat=aformat, playlist=it.get("playlist", ""), tags=it.get("tags"), fps=fps,
                        folder=target_dir(s, kind, it, folder))
                if clip:
                    j.clip_start, j.clip_end, j.fine_trim = clip["start"], clip["end"], clip["fine"]
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
                    if j.fine_trim:   # a fresh download gets trimmed afresh
                        j.trim_start = j.trim_end = None
            elif act == "cancel":
                if j.status in ACTIVE:
                    self._stop(j, "cancel")
                elif j.status in ("queued", "paused", "trim"):
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

    def add_export(self, src, edit_id, length, start, end, spec, folder):
        """Queue an export from the Edit tab. start/end: seconds into the file (for video, frame n is at
        n / fps), or None for the whole of it."""
        s = settings_mod.load()
        opts = export_options(spec, s)
        start, end = _clean_range(start, end, length)
        stem = os.path.splitext(os.path.basename(src))[0]
        with self.lock:
            span = (end if end is not None else float(length or 0)) - (start or 0.0)
            j = Job(url="", site="Edit", source=src, edit_id=edit_id, duration=round(span, 3) or None, folder=folder,
                    trim_start=start, trim_end=end, **opts)
            if opts["kind"] == "video":
                j.quality = "%dp" % j.height if j.height else ""
            j.title = j.filename or stem
            self.jobs[j.id] = j
            self.order.append(j.id)
            self._changed()
        self._pump()
        return j.id

    def set_trim(self, jid, start, end, spec=None, folder=""):
        """The trim window's first and last frames (seconds into the downloaded part, end not included),
        and from the Edit tab, what to save them as."""
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j.status != "trim":
                raise RuntimeError("This download isn't waiting to be trimmed")
            start, end = float(start), float(end)
            if not (0 <= start < end <= (j.clip_len or end) + 0.5):
                raise RuntimeError("Pick an In point before the Out point")
            if spec:
                opts = export_options(spec, settings_mod.load())
                if opts["kind"] == "video" and not j.clip_fps:
                    raise RuntimeError("This part is sound only, so it can only be saved as audio")
                for k, v in opts.items():
                    if k != "filename" or v:
                        setattr(j, k, v)
                if opts["kind"] == "video":
                    j.quality = "%dp" % j.height if j.height else j.quality
                if folder:
                    j.folder = folder
            j.trim_start, j.trim_end = start, end
            self._reset(j)
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
        resumed = bool(not j.source and j.raw_file and os.path.exists(j.raw_file) and (j.converts() or j.is_clip()))
        if j.source:
            result, err = self._export(j, s)
        elif resumed:
            result, err = "ok", ""      # downloaded earlier; only the trim / conversion is left
            j.filepath = j.raw_file
        else:
            allowed = login_allowed(s, j.url)
            with_login = allowed and s["cookies_mode"] == "always"
            result, err = self._download(j, s, with_login)
            # A refusal logged out goes straight to the login: asking again logged out rarely helps.
            if result == "error" and allowed and not with_login and REFUSED_RE.search(err):
                j.log.append("== %s refused NooNoo logged out (%s): trying with your %s login"
                             % (_host(j.url), err.strip()[:120], login_label(s)))
                with self.lock:
                    j.status, j.stage, j.progress, j.part, j._cur, j._base = "starting", "Signing in...", 0, 0, None, 0
                    self._changed()
                with_login = True
                result, err = self._download(j, s, True)
            result, err = self._retry_refused(j, s, with_login, result, err)
            if result == "error" and j.is_clip() and not j._full:
                # some sites (and live streams) won't send just a part: get the whole video and cut it here
                j.log.append("== Couldn't download just this part (%s): downloading the whole video instead" % err.strip()[:120])
                j._full = True
                with self.lock:
                    j.status, j.stage, j.progress, j.part, j._cur, j._base = "starting", "Getting the whole video...", 0, 0, None, 0
                    self._changed()
                result, err = self._download(j, s, with_login)
            if with_login and result == "ok":
                j.used_login = login_label(s)
        if result == "ok" and j.is_clip() and not j.source:
            if not resumed:
                result, err = self._prepare_clip(j, s)
            if result == "ok" and j.trim_end is None:
                if j.fine_trim:
                    result = "trim"
                else:   # the typed range, to the nearest frames
                    j.trim_start = max(0.0, j.clip_start - j.clip_offset)
                    j.trim_end = max(j.trim_start + 0.001, j.clip_end - j.clip_offset)
            if result == "ok":
                result, err = self._cut(j, s)
        if result == "ok" and j.filepath and j.filepath.startswith(str(TEMP_DIR)) and j.tags:
            result, err = self._retag(j, s, j.filepath)
        if result == "ok" and j.kind == "audio" and j.fps_rate() and j.filepath.lower().endswith(".wav") \
                and j.filepath.startswith(str(TEMP_DIR)):
            result, err = self._stamp_bwf(j, j.filepath)
        if result == "ok" and j.filename and j.filepath and j.filepath.startswith(str(TEMP_DIR)):
            result, err = self._apply_filename(j)
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
            elif result == "trim":
                j.status, j.stage, j.progress = "trim", "", 100.0
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
        if j.status == "trim" and s["notify_done"]:
            notify("Ready to trim", "%s: double-click it in Monitor to set the exact frames." % j.title)
        elif j.status == "done" and s["notify_done"]:
            notify("Export complete" if j.source else "Download complete", j.title)
        elif j.status == "error" and s["notify_done"]:
            notify("Export failed" if j.source else "Download failed", "%s: %s" % (j.title or j.url, j.error))
        self._pump()

    def _retry_refused(self, j, s, with_login, result, err):
        """Try again, a little later, when the site turned this download away rather than failing it."""
        for n, wait in enumerate(RETRY_WAITS, 1):
            if result != "error" or not TRANSIENT_RE.search(err):
                break
            j.log.append("== %s refused this time (%s). Trying again in %ds (%d of %d)"
                         % (_host(j.url), err.strip()[:120], wait, n, len(RETRY_WAITS)))
            with self.lock:
                j.status, j.stage, j.progress, j.part, j._cur, j._base = "starting", "Site busy, trying again...", 0, 0, None, 0
                j.proc = None
                self._changed()
            deadline = time.time() + wait
            while time.time() < deadline and not j._intent:
                time.sleep(0.25)
            if j._intent:
                return ("paused" if j._intent == "pause" else "cancelled"), ""
            result, err = self._download(j, s, with_login)
        return result, err

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

    def _convert(self, j, s, clip=None, name=None, src=None, seek=None):
        """ffmpeg pass to H.264 / H.265 / DNxHD / DNxHR / ProRes. Returns (ok|paused|cancelled|error, message).
        clip = (first frame, frame after the last, their times in seconds): keep only those frames.
        src = the file to convert (default: the download). seek = start reading it there (seconds into it),
        so a short stretch of a long file doesn't mean decoding everything before it; clip then counts
        frames and times from that point."""
        spec = VIDEO_FORMATS[j.vformat]
        src = src or j.raw_file
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
        if not video and (clip or j.source):
            return "error", "There's no picture in %s" % os.path.basename(src)
        if not clip and not j.source and (not video or (
                j.vformat == "h264" and main.get("codec_name") == "h264" and src.endswith(".mp4") and not j.fps_rate()
                and not j.height and not j.target_mb and all(a.get("codec_name") in ("aac", "mp3") for a in audio))):
            return self._deliver(j, src)  # audio-only source, or already plays everywhere

        work = TEMP_DIR / j.id
        work.mkdir(parents=True, exist_ok=True)
        # a leading dot, because a chosen file name never has one, so src can't be called this too
        dst = str(work / (".converting." + spec["ext"]))
        duration = float(probe.get("format", {}).get("duration") or j.duration or 0)
        fps = main.get("avg_frame_rate") or main.get("r_frame_rate") or "30/1"
        if fps in ("0/0", "0/1"):
            fps = main.get("r_frame_rate") or "30/1"
        out_rate = j.fps_rate() or fps

        cmd = [str(tools.path("ffmpeg")), "-hide_banner", "-nostdin", "-y"]
        if seek is not None:
            cmd += ["-ss", "%.6f" % seek]
            if clip:   # read no further than needed
                cmd += ["-t", "%.6f" % (clip[3] + 1)]
        cmd += ["-i", src, "-map", "0:%d" % main["index"], "-map", "0:a:0?", "-map_metadata", "0"]
        # picture size: even numbers always; a chosen height is the short side (so vertical video stays vertical)
        h = int(j.height or 0)
        size = ("scale='if(gte(iw,ih),-2,%d)':'if(gte(iw,ih),%d,-2)':flags=lanczos" % (h, h) if h
                else "scale=trunc(iw/2)*2:trunc(ih/2)*2")
        vf, af, extra = [size], None, []
        if (clip or j.source) and s["embed_metadata"]:
            extra += ["-metadata", "title=" + j.title]
        if clip:
            # Counting decoded frames is exact whatever the timestamps say; the sound is cut at those
            # frames' own times. Both happen before any frame-rate change.
            first, after, t_in, t_out = clip
            duration = t_out - t_in
            pics = []
            af = _sound_at(t_in, duration)
            vf.insert(0, "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS" % (first, after))
            extra += ["-map_chapters", "-1"]
        target = None
        if "dnxhd" in spec:
            rate = nearest_dnxhd_rate(out_rate)
            mbps = DNXHD_RATES[rate][spec["dnxhd"]]
            vf[-1] = ("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                      "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1")   # letterbox / pillarbox anything that isn't 16:9
            venc = ["-c:v", "dnxhd", "-profile:v", "dnxhd", "-b:v", "%dM" % mbps, "-pix_fmt", "yuv422p",
                    "-r", rate, "-fps_mode", "cfr"]
            aenc = _PCM
            extra += ["-map_chapters", "-1", "-dn", "-movflags", "+write_colr"]
        elif spec.get("edit"):
            venc = spec["v"] + ["-r", out_rate, "-fps_mode", "cfr"]
            aenc = _PCM
            extra += ["-map_chapters", "-1", "-dn", "-movflags", "+write_colr"]
        else:  # H.264 / H.265 on the Mac's media engine; bitrate scaled to picture size
            hevc = bool(spec.get("hevc"))
            w0, h0 = main.get("width") or 1920, main.get("height") or 1080
            if h:
                k = h / max(1, min(w0, h0))
                w0, h0 = w0 * k, h0 * k
            rate = _rate_value(out_rate) or 30.0
            kbps = max(2500, int(w0 * h0 * min(rate, 60) * 0.12 / 1000))
            if hevc:
                kbps = max(1800, int(kbps * 0.6))
            venc = (["-r", j.fps_rate(), "-fps_mode", "cfr"] if j.fps_rate() else [])
            tag = ["-tag:v:0", "hvc1" if hevc else "avc1"]
            hw = ["-c:v:0", "hevc_videotoolbox" if hevc else "h264_videotoolbox", "-b:v:0", "%dk" % kbps]
            if not hevc:
                hw += ["-profile:v:0", "high"]
            ok_audio = audio and audio[0].get("codec_name") in ("aac", "mp3") and not clip   # a cut can't copy
            aenc = ["-c:a", "copy"] if ok_audio else ["-c:a", "aac", "-b:a", "256k"]
            if j.target_mb:
                target = self._size_budget(j, duration, bool(audio))
                if isinstance(target, str):
                    return "error", target
                aenc = ["-c:a", "aac", "-b:a", "%dk" % target[1]]
                hw = ["-c:v:0", "libx265" if hevc else "libx264", "-b:v:0", "%dk" % target[0], "-preset", "medium"]
            venc += hw + ["-pix_fmt:v:0", "yuv420p"] + tag
            extra += ["-movflags", "+faststart"]
            if pics:  # keep the cover art the download embedded
                extra += ["-map", "0:%d" % pics[0]["index"], "-c:v:1", "copy", "-disposition:v:1", "attached_pic"]

        def build(v, pass_args=None, to=dst):
            if to == os.devnull:   # a measuring pass: picture only
                return cmd + ["-filter:v:0", ",".join(vf)] + v + pass_args + ["-an", "-f", "null", "-progress", "pipe:1", "-nostats", to]
            c = cmd + ["-filter:v:0", ",".join(vf)] + (["-filter:a:0", af] if af else []) + v
            return c + (pass_args or []) + aenc + extra + ["-progress", "pipe:1", "-nostats", to]

        label = spec["label"].split(" (")[0]
        if "dnxhd" in spec:
            label = "DNxHD %d" % DNXHD_RATES[nearest_dnxhd_rate(out_rate)][spec["dnxhd"]]
        verb = "Exporting" if j.source else "Converting"
        with self.lock:
            j.status, j.stage, j.progress, j.speed, j.eta = "converting", "%s to %s" % (verb, label), 0, None, None
            self._changed()
        if target:
            rc, err = self._two_pass(j, build, venc, duration, label, verb, dst, target)
        else:
            rc, err = self._ffmpeg(j, build(venc), duration, label, verb=verb)
            if rc != 0 and not j._intent and not spec.get("edit"):
                # no hardware encoder (old Intel Mac, odd size): software x264 / x265 instead
                j.log.append("== hardware encoder failed, retrying in software")
                i = venc.index("-c:v:0")
                sw = ["libx265", "-crf", "22"] if spec.get("hevc") else ["libx264", "-crf", "18"]
                venc = venc[:i] + ["-c:v:0", sw[0], sw[1], sw[2], "-preset", "fast"] + \
                    [a for a in venc[i + 4:] if a not in ("-profile:v:0", "high")]
                rc, err = self._ffmpeg(j, build(venc), duration, label, verb=verb)
        if j._intent or rc != 0:
            try:
                os.remove(dst)
            except OSError:
                pass
            if j._intent:
                return ("paused" if j._intent == "pause" else "cancelled"), ""
            return "error", "%s to %s failed: %s" % ("Export" if j.source else "Conversion", label, err)
        try:
            final = _place(dst, j.folder, (name or os.path.splitext(os.path.basename(src))[0]) + "." + spec["ext"])
        except OSError as e:
            return "error", "Couldn't save into %s: %s" % (j.folder, e)
        if s["keep_original"] and not clip and not j.source:
            self._deliver(j, src)
        j.filepath = final
        if not j.source:
            j.raw_file = ""
        return "ok", ""

    def _size_budget(self, j, duration, has_audio):
        """Bitrates (video, audio kb/s) that fill target_mb, a little under (containers add a few %),
        or why it can't be done."""
        if duration <= 0:
            return "NooNoo couldn't tell how long this is, so it can't aim for a file size"
        total = j.target_mb * 8000 * 0.96 / duration          # MB as Finder counts them: 1,000,000 bytes
        a = 0
        if has_audio:
            a = 160 if total > 3000 else 128 if total > 1000 else 96 if total > 400 else 64 if total > 150 else 32
        v = int(total - a)
        if v < 50:
            need = (50 + a) * duration / (8000 * 0.96)
            return ("%g MB is too small for %s of video. Make the part shorter or the size at least %d MB."
                    % (j.target_mb, _dur_text(duration), math.ceil(need)))
        return v, a

    def _two_pass(self, j, build, venc, duration, label, verb, dst, target):
        """Aim for a file size: x264 / x265 measure the video in a first pass, then share the bits out in the
        second. If the file still comes out too big, the second pass runs again a little smaller."""
        log = str(TEMP_DIR / j.id / "pass")
        x265 = "libx265" in venc
        def passes(n):
            return (["-x265-params", "pass=%d:stats=%s.x265" % (n, log)] if x265
                    else ["-pass", str(n), "-passlogfile", log])
        rc, err = self._ffmpeg(j, build(venc, passes(1), os.devnull), duration, label,
                               verb="Measuring (pass 1 of 2)", span=(0, 40))
        if rc != 0 or j._intent:
            return rc, err
        limit = j.target_mb * 1e6
        v_kbps = target[0]
        for attempt in range(3):
            i = venc.index("-b:v:0")
            venc[i + 1] = "%dk" % v_kbps
            rc, err = self._ffmpeg(j, build(venc, passes(2)), duration, label,
                                   verb="%s (pass 2 of 2)" % verb, span=(40, 100))
            if rc != 0 or j._intent:
                return rc, err
            got = os.path.getsize(dst)
            j.log.append("== %.2f MB (aiming for %g MB at %d kb/s video)" % (got / 1e6, j.target_mb, v_kbps))
            if got <= limit:
                return 0, ""
            v_kbps = int(v_kbps * limit / got * 0.95)
            if v_kbps < 30:
                break
        return 1, "it came out bigger than %g MB, even at the lowest quality" % j.target_mb

    # ---- parts of videos --------------------------------------------------
    def _prepare_clip(self, j, s):
        """A part has downloaded: measure it, and make the small copy the trim window plays."""
        work = TEMP_DIR / j.id
        src = j.filepath
        stem, ext = os.path.splitext(os.path.basename(src))
        ffmpeg, ffprobe = str(tools.path("ffmpeg")), str(tools.path("ffprobe"))
        a, b = j.clip_bounds()
        with self.lock:
            j.status, j.stage, j.speed, j.eta = "processing", "Measuring the part...", None, None
            self._changed()
        if j._full:   # take the same stretch out of the whole video, the way yt-dlp would have
            part = str(work / ("part" + ext))
            out = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-copyts", "-ss", "%.3f" % a, "-to", "%.3f" % b,
                                  "-i", src, "-map", "0", "-c", "copy", part],
                                 capture_output=True, text=True, errors="replace")
            if out.returncode != 0:
                j.log.append(out.stderr.strip()[-500:])
                return "error", "Couldn't take the part out of the downloaded video"
            os.remove(src)
            src = part
        j.filepath = j.raw_file = src
        try:
            probe = json.loads(subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", src],
                                              capture_output=True, text=True, timeout=60).stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return "error", "Couldn't read the downloaded part: %s" % e
        streams = probe.get("streams", [])
        video = [x for x in streams if x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")]
        has_audio = any(x.get("codec_type") == "audio" for x in streams)
        # The part keeps the video's own timestamps (seconds into the whole video), but ffmpeg's filters
        # count from the file's first timestamp, t0, so cut times are measured from there.
        info = {"name": stem, "t0": float(probe.get("format", {}).get("start_time") or 0), "pts": []}
        j.clip_offset = info["t0"]   # where in the video the part starts; for video, its first frame
        if video and j.kind == "video":
            fr = video[0].get("avg_frame_rate") or "0/0"
            if _rate_value(fr) <= 0:
                fr = video[0].get("r_frame_rate") or "30/1"
            # every frame's own time, in the order ffmpeg hands them to the cut
            try:
                out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                      "frame=best_effort_timestamp_time", "-of", "csv=p=0", src],
                                     capture_output=True, text=True, timeout=900)
            except (OSError, subprocess.TimeoutExpired) as e:
                return "error", "Couldn't read the downloaded part's frames: %s" % e
            for line in out.stdout.splitlines():
                try:
                    info["pts"].append(float(line.split(",")[0]))
                except ValueError:
                    pass
            if not info["pts"]:
                return "error", "The downloaded part has no frames in it"
            j.clip_fps, j.clip_frames = fr, len(info["pts"])
            j.clip_len = len(info["pts"]) / _rate_value(fr)
            j.clip_offset = info["pts"][0]
        elif has_audio:
            j.clip_fps, j.clip_frames = "", 0
            j.clip_len = _audio_end(src) - info["t0"] or None   # the container's own duration can't be trusted here
        else:
            return "error", "The downloaded part has no picture or sound in it"
        with open(work / "clip.json", "w") as f:
            json.dump(info, f)
        if j.fine_trim and j.trim_end is None:
            return self._make_preview(j, src, has_audio, info["t0"])
        return "ok", ""

    def _make_preview(self, j, src, has_audio, t0=0.0):
        """The trim window's copy of the part: small, and with every frame kept and evenly spaced, so
        frame n plays at exactly n / fps and the window can tell the engine which frames it means."""
        work = TEMP_DIR / j.id
        ffmpeg = str(tools.path("ffmpeg"))
        with self.lock:
            j.status, j.stage, j.progress = "converting", "Making the trim preview...", 0
            self._changed()
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src]
        if j.clip_fps:
            n, _, d = j.clip_fps.partition("/")
            n, d = int(n), int(d or 1)
            dst = str(work / "preview.mp4")
            # frame n at n / fps, and the sound kept at its own time against the first frame
            cmd += ["-map", "0:v:0", "-map", "0:a:0?",
                    "-vf", "setpts=N*%d/%d/TB,scale=-2:'min(540,ih)',format=yuv420p" % (d, n),
                    "-af", _sound_at(j.clip_offset - t0),
                    "-fps_mode", "passthrough", "-video_track_timescale", str(n),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "12", "-bf", "0",
                    "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart"]
        else:
            dst = str(work / "preview.m4a")
            cmd += ["-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        cmd += ["-progress", "pipe:1", "-nostats", dst]
        rc, err = self._ffmpeg(j, cmd, j.clip_len, "preview", verb="Making the trim preview")
        if j._intent:
            return ("paused" if j._intent == "pause" else "cancelled"), ""
        if rc != 0:
            return "error", "Couldn't make the trim preview: %s" % err
        if has_audio:   # a picture of the sound, drawn under the timeline (so lined up with the frames)
            line = _sound_at(j.clip_offset - t0, j.clip_len) + "," if j.clip_fps else ""
            try:
                subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src, "-lavfi",
                                "[0:a:0]%saformat=channel_layouts=mono,showwavespic=s=1600x120:colors=0x2f5ba8" % line,
                                "-frames:v", "1", str(work / "wave.png")], capture_output=True, timeout=300)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return "ok", ""

    def _cut(self, j, s):
        """Keep only the frames (or stretch of sound) picked for a part, encoded once in the chosen format."""
        try:
            with open(TEMP_DIR / j.id / "clip.json") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return "error", "The downloaded part's details are missing. Press Retry to download it again."
        if not j.clip_fps:
            return self._cut_audio(j, s, info)
        fps = _rate_value(j.clip_fps)
        pts, t0 = info["pts"], info["t0"]
        first = max(0, min(len(pts) - 1, int(round(j.trim_start * fps))))
        after = max(first + 1, min(len(pts), int(round(j.trim_end * fps))))
        t_in = pts[first] - t0
        t_out = (pts[after] if after < len(pts) else pts[-1] + 1 / fps) - t0
        j._clip_label = _clip_label(pts[first], pts[after - 1], fps)   # frame times are times in the video
        if j.kind == "audio":   # the sound under those frames only
            return self._encode_audio(j, s, j.raw_file, t_in, t_out, info["name"] + j._clip_label)
        j.log.append("== Keeping frames %d to %d of the part (%d frames)" % (first, after - 1, after - first))
        return self._convert(j, s, clip=(first, after, t_in, t_out), name=j.filename or info["name"] + j._clip_label)

    def _cut_audio(self, j, s, info):
        t_in, t_out = j.trim_start, j.trim_end
        j._clip_label = _clip_label(j.clip_offset + t_in, j.clip_offset + t_out)
        return self._encode_audio(j, s, j.raw_file, t_in, t_out, info["name"] + j._clip_label)

    def _encode_audio(self, j, s, src, t_in, t_out, name, seek=False):
        """The sound from t_in to t_out (seconds from the start of src), encoded once in the chosen audio
        format, into the job's workspace. seek: jump there first (quick on a long file)."""
        fmt = j.aformat if j.aformat in AUDIO_ENCODERS else ""
        if not fmt:   # "Original": the same kind of file the site sent
            try:
                codec = subprocess.run([str(tools.path("ffprobe")), "-v", "error", "-select_streams", "a:0", "-show_entries",
                                        "stream=codec_name", "-of", "csv=p=0", src], capture_output=True, text=True,
                                       timeout=60).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                codec = ""
            if not codec:
                return "error", "There's no sound in %s" % os.path.basename(src)
            fmt = {"aac": "m4a", "opus": "opus", "mp3": "mp3", "flac": "flac"}.get(codec, "m4a")
        ext, enc = AUDIO_ENCODERS[fmt]
        q = (s["audio_quality"] or "0").strip()
        dur = t_out - t_in
        if j.target_mb:
            if fmt not in ("mp3", "m4a", "opus"):
                return "error", "%s files can't be made to fit a size: pick MP3, M4A or Opus" % fmt.upper().split("_")[0]
            kbps = int(j.target_mb * 8000 * 0.96 / max(0.01, dur))
            if kbps < 32:
                return "error", "%g MB is too small for %s of sound. Make it at least %d MB." % (
                    j.target_mb, _dur_text(dur), math.ceil(32 * dur / (8000 * 0.96)))
            if fmt == "mp3":   # MP3 only has these rates, and rounds anything else up
                kbps = max([r for r in (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if r <= kbps])
                enc = enc + ["-b:a", "%dk" % kbps, "-id3v2_version", "3"]
            else:
                enc = enc + ["-b:a", "%dk" % min(int(kbps * 0.97), 256)]
        elif fmt == "mp3":
            enc = enc + (["-q:a", q] if q.isdigit() else ["-b:a", q]) + ["-id3v2_version", "3"]
        elif fmt in ("m4a", "opus"):
            enc = enc + ["-b:a", q if not q.isdigit() else ("256k" if fmt == "m4a" else "160k")]
        out_dir = TEMP_DIR / j.id / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = str(out_dir / (name + "." + ext))
        cmd = [str(tools.path("ffmpeg")), "-hide_banner", "-nostdin", "-y"]
        if seek:
            cmd += ["-ss", "%.6f" % t_in, "-t", "%.6f" % dur, "-i", src, "-af", "asetpts=PTS-STARTPTS"]
        else:
            cmd += ["-i", src, "-af", "atrim=start=%.6f:end=%.6f,asetpts=PTS-STARTPTS" % (t_in, t_out)]
        cmd += ["-map", "0:a:0", "-map_metadata", "0", "-vn"] + enc
        if s["embed_metadata"]:
            cmd += ["-metadata", "title=" + j.title]
        cmd += ["-progress", "pipe:1", "-nostats", dst]
        verb = "Exporting" if j.source else "Cutting"
        with self.lock:
            j.status, j.stage, j.progress, j.speed, j.eta = "converting", verb + " the sound...", 0, None, None
            self._changed()
        rc, err = self._ffmpeg(j, cmd, dur, "part", verb=verb)
        if j._intent or rc != 0:
            try:
                os.remove(dst)
            except OSError:
                pass
            if j._intent:
                return ("paused" if j._intent == "pause" else "cancelled"), ""
            return "error", "Couldn't %s the sound: %s" % ("export" if j.source else "cut", err)
        j.filepath = dst
        return "ok", ""

    def _export(self, j, s):
        """An export from the Edit tab: the frames (or stretch of sound) picked, from a file already on the
        Mac, in the format, size and frame rate chosen. The file itself is only read."""
        from . import editor
        src = j.source
        if not os.path.isfile(src):
            return "error", "%s isn't there any more (moved or deleted?)" % src
        info = editor.load_info(j.edit_id, src)
        if not info:
            return "error", "NooNoo hasn't measured this file yet. Open it in the Edit tab, then export again."
        with self.lock:
            j.status, j.stage, j.speed, j.eta = "processing", "Getting ready...", None, None
            self._changed()
        stem = os.path.splitext(os.path.basename(src))[0]
        tc0 = info.get("tc0") or 0.0
        if info.get("fps"):
            fps, pts = _rate_value(info["fps"]), info["pts"]
            n = len(pts)
            first = 0 if j.trim_start is None else max(0, min(n - 1, int(round(j.trim_start * fps))))
            after = n if j.trim_end is None else max(first + 1, min(n, int(round(j.trim_end * fps))))
            t_in, t_out = pts[first], (pts[after] if after < n else pts[-1] + 1 / fps)
            label = _clip_label(tc0 + pts[first], tc0 + pts[after - 1], fps) if first > 0 or after < n else ""
        else:
            fps = 0
            t_in = j.trim_start or 0.0
            t_out = j.trim_end if j.trim_end is not None else info["len"]
            first, after = 0, 0
            label = _clip_label(tc0 + t_in, tc0 + t_out) if t_in > 0.0005 or t_out < info["len"] - 0.0005 else ""
        name = j.filename or export_name(stem, label, j)
        with self.lock:   # Monitor lists it by the name it's saved as, and its real picture size
            j.title = name
            if j.kind == "video" and info.get("height"):
                short = min(info["height"], info.get("width") or info["height"])
                j.quality = "%dp" % (j.height or short) if not VIDEO_FORMATS[j.vformat].get("dnxhd") else "1080p"
            self._changed()
        if j.kind == "audio":
            j.log.append("== Exporting the sound from %.3f s to %.3f s of %s" % (t_in, t_out, src))
            return self._encode_audio(j, s, src, t_in, t_out, name, seek=True)
        if not fps:
            return "error", "There's no picture in %s to export as video" % os.path.basename(src)
        j.log.append("== Exporting frames %d to %d of %s (%d frames)" % (first, after - 1, src, after - first))
        # Start reading halfway between the frame before In and In itself, so In is the first frame left.
        seek = (pts[first - 1] + pts[first]) / 2 if first > 0 else None
        base = seek or 0.0
        clip = (0, after - first, t_in - base, t_out - base) if seek is not None else (first, after, t_in, t_out)
        return self._convert(j, s, clip=clip, name=name, src=src, seek=seek)

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
        named = os.path.join(work, song_filename(s, tags, os.path.basename(src)) + _clip_suffix(j) + ext)
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

    def _apply_filename(self, j):
        """Swap the name yt-dlp gave the file for the one the user chose. Conversions then keep it."""
        named = os.path.join(os.path.dirname(j.filepath), j.filename + os.path.splitext(j.filepath)[1])
        try:
            os.replace(j.filepath, named)
        except OSError as e:
            return "error", "Couldn't name the file %s: %s" % (j.filename, e)
        j.filepath = named
        return "ok", ""

    def _deliver(self, j, src):
        """Move a finished file out of the job's workspace into its destination folder."""
        try:
            final = _place(src, j.folder, os.path.basename(src))
        except OSError as e:
            return "error", "Couldn't save into %s: %s" % (j.folder, e)
        j.filepath, j.raw_file = final, ""
        return "ok", ""

    def _ffmpeg(self, j, cmd, duration, label, verb="Converting", span=(0, 100)):
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
                        j.progress = round(min(99.9, span[0] + (span[1] - span[0]) * min(1, pos / duration)), 1)
                    if speed:
                        j.eta = max(0, (duration - pos) / speed) if duration else None
                        j.stage = "%s · %.1f× realtime" % (verb, speed)
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
                    j.title = j.filename or info.get("title") or j.title
                    j.site = info.get("extractor_key") or j.site
                    j.thumbnail = j.thumbnail or info.get("thumbnail") or ""
                    j.duration = j.duration or info.get("duration")
                    j.uploader = j.uploader or info.get("uploader") or ""
                    fid = info.get("format_id") or ""
                    j.parts = fid.count("+") + 1 if fid else 1
                    if not j.is_clip():   # the whole video's size, not the part's
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
    (r"HTTP Error 403|HTTP Error 429|Too Many Requests", "The site kept turning NooNoo away. This usually passes in a few minutes, so press Retry then."),
    (r"No video could be found", "This post has no video in it."),
    (r"Cannot parse data", "yt-dlp couldn't find the video on this page. If the site needs you signed in, "
                           "Tools > Options > Accounts can lend NooNoo your browser login. Otherwise the site has changed, "
                           "and NooNoo installs yt-dlp's fix by itself once it's released."),
    (r"Unsupported video frame rate|frame rate", "MXF needs a standard frame rate - pick one under Options > Video, or use .mov."),
]


def _friendly_error(line, s=None, url=""):
    msg = re.sub(r"^ERROR:\s*", "", line).strip()
    msg = re.sub(r"\s*Use --cookies.*$", "", msg)  # replaced by our own hint below
    msg = re.sub(r"\s*See\s+https://github.com/yt-dlp/yt-dlp/wiki/FAQ\S*.*$", "", msg)
    msg = re.sub(r";?\s*please report this issue on\s.*$", "", msg)   # meant for yt-dlp users, not NooNoo's
    if s is not None and LOGIN_RE.search(msg):
        hint = _login_hint(s, url)
        if re.search(r"not a bot", msg, re.I):   # YouTube checking this connection, not this video
            hint = "YouTube is limiting this connection for now. It usually clears in a few minutes, so press Retry then. " + hint
        return "%s  %s" % (msg, hint.strip()) if hint.strip() else msg
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


_MEDIA_SUFFIX = re.compile(r"\.(mp4|m4v|mov|mkv|webm|avi|mxf|mp3|m4a|aac|opus|ogg|flac|wav|aiff?)$", re.I)
_FILENAME_SWAPS = str.maketrans({"/": "⧸", "\\": "⧹", ":": "：", "*": "＊", "?": "？", '"': "＂",
                                  "<": "＜", ">": "＞", "|": "｜"})


def clean_filename(s, name):
    """A file name that's safe to save, without path separators or anything else the filesystem refuses."""
    name = str(name or "").strip()
    if s["restrict_filenames"]:
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        name = re.sub(r"[^\w.-]+", "_", name).strip("_.")
    else:
        name = re.sub(r"[\x00-\x1f]", "", name.translate(_FILENAME_SWAPS)).strip().lstrip(".")
    return name.encode()[:200].decode("utf-8", "ignore").rstrip(" .")


def custom_filename(s, name):
    """A name typed for one download. The file's real type sets the extension, so a typed one is dropped."""
    return clean_filename(s, _MEDIA_SUFFIX.sub("", str(name or "").strip()))


def song_filename(s, tags, fallback):
    """Just the Spotify song title - never the YouTube upload's title, artist or id."""
    return clean_filename(s, tags.get("title")) or os.path.splitext(fallback)[0]


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


def _sound_at(start, length=None):
    """ffmpeg audio filter: the sound from `start` (seconds into the file), as time 0, and `length` long.
    A part's picture starts at a keyframe but its sound where it was asked to, so either can begin first:
    what's missing is filled with silence rather than letting the sound slide out of sync."""
    f = "asetpts=PTS-%.6f/TB,atrim=start=0%s,aresample=async=1:first_pts=0" % (start, ":end=%.6f" % length if length else "")
    return f + (",apad=whole_dur=%.6f" % length if length else "")


def _dur_text(sec):
    sec = int(round(sec))
    return "%d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60) if sec >= 3600 else "%d:%02d" % (sec // 60, sec % 60)


def _audio_end(src):
    """Where a file's sound ends, in its own timestamps (read from the packets, which is quick)."""
    try:
        out = subprocess.run([str(tools.path("ffprobe")), "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "packet=pts_time,duration_time", "-of", "csv=p=0", src],
                             capture_output=True, text=True, timeout=300).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    end = 0.0
    for line in out.splitlines():
        try:
            pts, _, dur = line.partition(",")
            end = max(end, float(pts) + float(dur.strip(",") or 0))
        except ValueError:
            pass
    return end


def _clean_range(start, end, length):
    """In and Out from the Edit tab: seconds, or None for the start / end of the file."""
    try:
        a = None if start is None else float(start)
        b = None if end is None else float(end)
    except (TypeError, ValueError):
        raise RuntimeError("In and Out aren't times NooNoo understands")
    if (a is not None and a < 0) or (a is not None and b is not None and b <= a) \
            or (b is not None and length and b > float(length) + 0.5):
        raise RuntimeError("Pick an In point before the Out point")
    return a, b


def _clean_clip(clip):
    """{"start", "end", "fine"} from the Download Part window, checked."""
    if not clip:
        return None
    try:
        a, b = float(clip.get("start")), float(clip.get("end"))
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("The part's start and end aren't times NooNoo understands")
    if a < 0 or b <= a:
        raise RuntimeError("The part has to end after it starts")
    return {"start": a, "end": b, "fine": bool(clip.get("fine", True))}


def _tc(sec, fps=None):
    """Timecode for a file name: HH.MM.SS.FF at a frame rate (non-drop), else HH.MM.SS.mmm."""
    sec = max(0.0, sec)
    if fps:
        nominal = int(round(fps))
        frames = int(round(sec * fps))
        whole, ff = divmod(frames, nominal)
        return "%02d.%02d.%02d.%02d" % (whole // 3600, whole // 60 % 60, whole % 60, ff)
    ms = int(round(sec * 1000))
    whole, ms = divmod(ms, 1000)
    return "%02d.%02d.%02d.%03d" % (whole // 3600, whole // 60 % 60, whole % 60, ms)


def _clip_label(first, last, fps=None):
    """ [in-out] for a part's file name; with a frame rate, last is the last frame kept."""
    return " [%s-%s]" % (_tc(first, fps), _tc(last, fps))


HEIGHTS = (0, 2160, 1440, 1080, 720, 540, 480, 360, 240)
FPS_CHOICES = ("source", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60")


def export_name(stem, label, j):
    """An export's file name when none was typed: the source's, the timecode it covers, and what changed."""
    desc = []
    if j.height and j.kind == "video":
        desc.append("%dp" % j.height)
    if j.fps and j.fps != "source" and j.kind == "video":
        desc.append("%s fps" % j.fps)
    if j.target_mb:
        desc.append("%g MB" % j.target_mb)
    if not desc and not label:
        desc.append(VIDEO_FORMATS[j.vformat]["label"].split(" (")[0].replace("MP4 · ", "") if j.kind == "video"
                    else AUDIO_FORMATS.get(j.aformat, "audio"))
    return stem + label + (" (%s)" % ", ".join(desc) if desc else "")


def export_options(spec, s):
    """What to export as, from the Edit tab, checked: kind, format, size, frame rate, file-size target."""
    spec = spec or {}
    kind = "audio" if spec.get("kind") == "audio" else "video"
    out = {"kind": kind}
    if kind == "video":
        out["vformat"] = spec.get("vformat") if spec.get("vformat") in VIDEO_FORMATS else s["video_format"]
    else:
        out["aformat"] = spec.get("aformat") if spec.get("aformat") in AUDIO_FORMATS else s["audio_format"]
    try:
        out["height"] = int(spec.get("height") or 0)
        out["target_mb"] = round(float(spec.get("target_mb") or 0), 2)
    except (TypeError, ValueError):
        raise RuntimeError("The size settings aren't numbers NooNoo understands")
    if out["height"] not in HEIGHTS:
        raise RuntimeError("Pick one of the listed picture sizes")
    if not 0 <= out["target_mb"] <= 100000:
        raise RuntimeError("Type a file size between 0.1 and 100,000 MB")
    fps = str(spec.get("fps") or "source")
    if fps not in FPS_CHOICES:
        raise RuntimeError("Pick one of the listed frame rates")
    out["fps"] = fps
    if kind == "video" and out["target_mb"] and VIDEO_FORMATS[out["vformat"]].get("edit"):
        raise RuntimeError("Editing formats like DNxHD and ProRes have a fixed quality, so they can't be made "
                           "to fit a size. Pick H.264 or H.265")
    out["filename"] = custom_filename(s, spec.get("filename"))
    return out


def _clip_suffix(j):
    return getattr(j, "_clip_label", "") if j.is_clip() else ""


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
    clip = j.is_clip()
    # naming a copy means wanting that copy; a part of a video doesn't make the whole one "downloaded"
    if s["skip_existing"] and not j.force and not j.filename and not clip:
        a += ["--download-archive", _archive(j)]
    if clip and not j._full:
        # -copyts keeps the video's own timestamps, so every frame of the part says where it came from.
        # Without it each stream is restarted from its own nearest keyframe, and an MKV part's picture,
        # sound and position in the video no longer line up in any way NooNoo can tell.
        a += ["--download-sections", "*%.3f-%.3f" % j.clip_bounds(), "--downloader-args", "ffmpeg:-copyts"]

    thumb_ok = True
    if j.kind == "audio" and clip:
        a += ["-f", "ba/b"]   # kept as it comes: NooNoo cuts and encodes it once, in _cut_audio
        thumb_ok = False
    elif j.kind == "audio":
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
        edit = spec.get("edit") or clip   # subtitles and chapters would no longer line up with a part
        thumb_ok = not edit
        if s["subtitles"] and not edit:
            a += ["--write-subs", "--sub-langs", s["sub_langs"] or "en.*", "--embed-subs"]
        if s["embed_chapters"] and not edit:
            a.append("--embed-chapters")
    if s["embed_thumbnail"] and thumb_ok and not j.tags:   # Spotify songs get their album cover in _retag
        a += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    if s["embed_metadata"] and not clip:   # it re-saves the file, losing the part's timestamps: tagged in the cut
        a.append("--embed-metadata")
    if s["sponsorblock"] and not clip:   # cutting sponsor bits out would move the frames the user picked
        a += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
    # save() refuses these, but settings.json can be edited, or predate the allowlist.
    extra, refused = settings_mod.split_extra_args(s["extra_args"])
    a += extra
    if refused:
        j.log.append("Ignored extra yt-dlp options NooNoo doesn't allow: " + " ".join(refused))
    a += ["--", j.url]
    return a
