"""User options, persisted as JSON next to the tools."""
import json
import threading
from pathlib import Path

from .tools import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULTS = {
    # Saving
    "output_dir": str(Path.home() / "Downloads" / "NooNoo"),
    "folders": "",                    # favourite destinations, one path per line
    "current_folder": "",             # where new downloads go ("" = output_dir)
    "organize": "none",               # none | site | uploader | playlist | date  - automatic subfolder
    "filename_template": "%(title).200B.%(ext)s",
    "filename_id": False,             # add the site's video id: "Title [dQw4w9WgXcQ].mp4"
    "restrict_filenames": False,
    "skip_existing": False,           # --download-archive, kept per folder + format
    # Video
    "video_quality": "best",          # best | 2160 | 1440 | 1080 | 720 | 480 | 360
    "video_format": "h264",           # key of engine.VIDEO_FORMATS (H.264 MP4, original, DNxHR, ProRes...)
    "edit_fps": "source",             # frame rate for converted video: source | 23.976 | 24 | 25 | 29.97 | 30 | 50 | 59.94 | 60
    "keep_original": False,           # keep the downloaded file after converting it
    # Audio
    "audio_format": "mp3",            # mp3 | m4a | opus | flac | wav | wav_avid | best
    "audio_quality": "0",             # yt-dlp VBR scale 0 (best) - 10, or e.g. 320K
    # Speed
    "max_concurrent": 3,
    "fragments": 8,                   # parallel fragment downloads per file (-N)
    "rate_limit": "",                 # e.g. 5M, empty = unlimited
    "retries": 10,
    # Extras
    "embed_thumbnail": True,
    "embed_metadata": True,
    "embed_chapters": True,
    "subtitles": False,
    "sub_langs": "en.*",
    "sponsorblock": False,
    "playlist_items_limit": 200,      # rows to load when a playlist/channel URL is searched
    "search_results": 30,
    "fetch_details": True,            # look up upload date / resolution / size for each search result
    # Accounts / network
    "cookies_browser": "",            # "", chrome, safari, firefox, brave, edge, chromium, opera, vivaldi
    "cookies_profile": "",            # browser profile (e.g. "Profile 2") - a separate profile limits exposure
    "cookies_sites": "",              # comma list of COOKIE_SITES keys the login may be used on (opt-in)
    "cookies_custom": "",             # extra domains, comma separated
    "cookies_mode": "fallback",       # fallback: only after a site refuses logged-out | always
    "proxy": "",
    # Advanced
    "extra_args": "",
    # Behaviour
    "instant_download": False,        # pasting a single URL downloads it straight away
    "default_kind": "video",          # what double-click / Enter downloads
    "notify_done": True,
    "auto_update_check": True,        # look for a newer NooNoo on launch and every few hours
    "theme": "blue",                  # blue | olive | silver  (the three Luna schemes)
    "thumbs": "small",                # thumbnail column in search results: off | small | large
    "columns": "",                    # JSON: {table: {"order": [...], "hidden": [...]}} from the column chooser
    "xp_frame": True,                 # draw the XP title bar instead of the macOS one
}

# Sites a browser login can be shared with, and the hosts each covers.
COOKIE_SITES = {
    "youtube": ("YouTube", ["youtube.com", "youtu.be"]),
    "instagram": ("Instagram", ["instagram.com"]),
    "facebook": ("Facebook", ["facebook.com", "fb.watch", "fb.com"]),
    "x": ("X / Twitter", ["x.com", "twitter.com"]),
    "tiktok": ("TikTok", ["tiktok.com"]),
    "reddit": ("Reddit", ["reddit.com", "redd.it"]),
    "vimeo": ("Vimeo", ["vimeo.com"]),
    "twitch": ("Twitch", ["twitch.tv"]),
    "soundcloud": ("SoundCloud", ["soundcloud.com"]),
    "patreon": ("Patreon", ["patreon.com"]),
}

_lock = threading.Lock()


def load():
    data = dict(DEFAULTS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return data
    data.update({k: v for k, v in raw.items() if k in DEFAULTS})
    old_formats = {"original": "h264", "mkv": "h264", "prores_hq": "dnxhr_hq", "prores": "dnxhr_sq",
                   "prores_lt": "dnxhr_sq", "prores_proxy": "dnxhr_lb", "dnxhr_hq_mxf": "dnxhr_hq"}
    data["video_format"] = old_formats.get(data["video_format"], data["video_format"])
    if raw.get("filename_template") == "%(title).180B [%(id)s].%(ext)s":  # old default had the id baked in
        data["filename_template"] = DEFAULTS["filename_template"]
    if raw.get("cookies_browser") and "cookies_sites" not in raw:
        # settings from before per-site opt-in: keep logins working, but only when a site insists
        data["cookies_sites"] = ",".join(COOKIE_SITES)
        data["cookies_mode"] = "fallback"
    return data


def save(data):
    clean = dict(DEFAULTS)
    for k, v in data.items():
        if k not in DEFAULTS:
            continue
        default = DEFAULTS[k]
        try:  # coerce to the default's type so the UI can post strings
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                v = int(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            v = default
        clean[k] = v
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2))
        tmp.replace(SETTINGS_FILE)
    return clean
