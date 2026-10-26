"""Spotify links, resolved to the same songs on YouTube.

Spotify's audio is DRM-protected, so we only read the track list (title, artists, length) from
Spotify's public embed page - no login or API key - and download each song's YouTube upload.
"""
import functools
import json
import re
import urllib.request

LINK_RE = re.compile(r"^(?:https?://)?(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(\w+)/(\w+)"
                     r"|spotify:(\w+):(\w+)$)", re.I)
SHORT_RE = re.compile(r"^(?:https?://)?spotify\.link/\S+$", re.I)
MUSIC = ("track", "album", "playlist", "artist")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"
FLAGS = ("live", "cover", "remix", "sped up", "slowed", "karaoke", "instrumental", "8d", "nightcore", "reverb")


def is_spotify(text):
    text = text.strip()
    return bool(LINK_RE.match(text) or SHORT_RE.match(text))


def _open(url, proxy):
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.build_opener(*handlers).open(req, timeout=20) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def _cover(e):
    imgs = (e.get("visualIdentity") or {}).get("image") or (e.get("coverArt") or {}).get("sources") or []
    imgs = [i for i in imgs if i.get("url")]
    return max(imgs, key=lambda i: i.get("maxWidth") or i.get("width") or 0)["url"] if imgs else ""


def _entity(kind, sid, proxy):
    page = _open("https://open.spotify.com/embed/%s/%s" % (kind, sid), proxy)[1]
    data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(data.group(1))["props"]["pageProps"]["state"]["data"]["entity"]


def _year(e):
    """Release date as YYYY-MM-DD (what ID3 / MP4 date tags take)."""
    iso = (e.get("releaseDate") or {}).get("isoString") or ""
    return iso[:10]


@functools.lru_cache(maxsize=2000)
def track_details(track_id, proxy=""):
    """A song's own album cover and release date - playlists only list titles and artists."""
    try:
        e = _entity("track", track_id, proxy)
    except (OSError, AttributeError, ValueError, KeyError, TypeError):
        return {"cover": "", "date": ""}
    return {"cover": _cover(e), "date": _year(e)}


def lookup(url, proxy=""):
    """Spotify link -> {"type", "name", "cover", "tracks": [{"title", "artists", "duration"}]}."""
    url = url.strip()
    if SHORT_RE.match(url):
        url = _open(url if "//" in url else "https://" + url, proxy)[0]
    m = LINK_RE.match(url)
    if not m:
        raise RuntimeError("Not a Spotify link")
    kind, sid = (m.group(1) or m.group(3)).lower(), m.group(2) or m.group(4)
    if kind not in MUSIC:
        raise RuntimeError("Spotify %ss can't be downloaded, only songs, albums, playlists and artists" % kind)
    try:
        e = _entity(kind, sid, proxy)
    except (OSError, AttributeError, ValueError, KeyError, TypeError):
        raise RuntimeError("Couldn't read this Spotify link (is it private, or mistyped?)")
    # an album's cover and year belong to every song on it; playlist/artist songs are looked up one by one
    shared = {"cover": _cover(e), "date": _year(e)} if kind in ("track", "album") else {"cover": "", "date": ""}
    if kind == "track":
        tracks = [dict(shared, id=sid, title=e.get("title") or e.get("name") or "",
                       artists=", ".join(a["name"] for a in e.get("artists") or [] if a.get("name")),
                       duration=e.get("duration"))]
    else:
        tracks = [dict(shared, id=(t.get("uri") or "").split(":")[-1], title=t.get("title") or "",
                       artists=t.get("subtitle") or "", duration=t.get("duration"))
                  for t in e.get("trackList") or [] if t.get("entityType", "track") == "track"]
    for t in tracks:
        t["duration"] = round(t["duration"] / 1000) if t["duration"] else None
    return {"type": kind, "name": e.get("name") or e.get("title") or "", "cover": _cover(e),
            "tracks": [t for t in tracks if t["title"]]}


def query(track):
    first = track["artists"].split(",")[0].strip()
    return "%s - %s" % (first, track["title"]) if first else track["title"]


def best_match(track, entries):
    """Pick the YouTube result most likely to be the Spotify recording, or None."""
    want = track["title"].lower()
    first = track["artists"].split(",")[0].strip().lower()
    best, best_score = None, None
    for i, e in enumerate(entries):
        if not e or not e.get("id"):
            continue
        title = (e.get("title") or "").lower()
        channel = (e.get("channel") or e.get("uploader") or "").lower()
        score = -i * 2.0                                   # YouTube's own ranking still counts
        dur, got = track["duration"], e.get("duration")
        if dur and got:
            off = abs(got - dur)
            if off > 15:
                continue
            score -= off * 2
        if first and channel in (first + " - topic", first, first + "vevo", first.replace(" ", "") + "vevo"):
            score += 20
        if want and want.split(" (")[0] in title:
            score += 10
        score -= sum(15 for w in FLAGS if w in title and w not in want)
        if best_score is None or score > best_score:
            best, best_score = e, score
    return best
