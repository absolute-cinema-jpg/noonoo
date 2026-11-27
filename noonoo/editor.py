"""The Edit tab's library: every finished download plus files added by hand, each measured once (every
frame's time) and given a small copy to play, in which frame n is at exactly n / fps. That copy is what
makes picking frames exact; exports are always made from the original file (see engine._export)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

from . import engine as engine_mod
from . import tools

EDIT_DIR = tools.DATA_DIR / "edit"
LIBRARY_FILE = EDIT_DIR / "library.json"
KEEP_DAYS = 14          # a copy nobody has opened for this long is deleted (it's made again if needed)
MEDIA_EXT = re.compile(r"\.(mp4|m4v|mov|mkv|webm|avi|mxf|mts|m2ts|ts|3gp|flv|wmv|mpg|mpeg|"
                       r"mp3|m4a|aac|opus|ogg|oga|flac|wav|aiff?|caf|wma)$", re.I)
PTS_RE = re.compile(r"pts_time:\s*(-?[\d.]+(?:e-?\d+)?)")
INFO_VERSION = 1


def sid_for(path):
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:10]


def _stamp(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def load_info(sid, path):
    """A file's measurements, if they're still about the file as it is now."""
    if not sid or not re.fullmatch(r"[0-9a-f]{10}", sid) or not path:
        return None
    try:
        info = json.loads((EDIT_DIR / sid / "info.json").read_text())
        ok = (info.get("v") == INFO_VERSION and info.get("path") == os.path.realpath(path)
              and {k: info.get(k) for k in ("size", "mtime")} == _stamp(path))
        return info if ok else None
    except (OSError, ValueError):
        return None


def _timecode(probe, fps):
    """A camera file's own start timecode (HH:MM:SS:FF), in seconds, so shown and named times match it."""
    tags = [probe.get("format", {}).get("tags", {})] + [x.get("tags", {}) for x in probe.get("streams", [])]
    for t in tags:
        tc = (t or {}).get("timecode")
        m = re.fullmatch(r"(\d+):(\d\d):(\d\d)[:;.](\d\d)", tc or "")
        if m and fps:
            h, mi, s, f = map(int, m.groups())
            return h * 3600 + mi * 60 + s + f / round(fps)
    return 0.0


def summary(info, sid):
    """What the page needs about a measured file (everything but the frame list)."""
    return {"sid": sid, "kind": "video" if info.get("fps") else "audio", "fps": info.get("fps") or "",
            "frames": len(info.get("pts") or []), "len": info.get("len"), "tc0": info.get("tc0", 0.0),
            "width": info.get("width"), "height": info.get("height"), "has_audio": info.get("has_audio"),
            "codec": info.get("codec", ""), "name": os.path.basename(info.get("path", "")),
            "path": info.get("path", ""), "size": info.get("size"), "wave": info.get("wave", False)}


def _job_length(j):
    """How long a finished download is: a part keeps only what was picked, not the whole video."""
    if not j:
        return None
    if j.get("trim_end") is not None:
        return j["trim_end"] - (j.get("trim_start") or 0)
    if j.get("clip_start") is not None and not j.get("source"):
        return j["clip_end"] - j["clip_start"]
    return j.get("duration")


class Library:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.added = []          # files added by hand: [{"path", "added"}]
        self.hidden = set()      # downloads taken off the list (the files themselves stay)
        self.preps = {}          # sid -> {"state": waiting|preparing|ready|error, "progress", "error", "proc"}
        self.gate = threading.Semaphore(2)   # copies made at once
        try:
            d = json.loads(LIBRARY_FILE.read_text())
            self.added = [a for a in d.get("added", []) if isinstance(a, dict) and a.get("path")]
            self.hidden = set(d.get("hidden", []))
        except (OSError, ValueError):
            pass
        threading.Thread(target=self._prune, daemon=True).start()

    def _save(self):
        try:
            EDIT_DIR.mkdir(parents=True, exist_ok=True)
            tmp = LIBRARY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"added": self.added, "hidden": sorted(self.hidden)}))
            tmp.replace(LIBRARY_FILE)
        except OSError:
            pass

    def _prune(self):
        if not EDIT_DIR.is_dir():
            return
        cutoff = time.time() - KEEP_DAYS * 86400
        for d in EDIT_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # ---- the list ------------------------------------------------------------
    def items(self):
        """Parts waiting to be trimmed, finished downloads and exports, then files added by hand."""
        out, seen = [], set()
        snap = self.engine.snapshot()["jobs"]
        for j in reversed(snap):
            if j["status"] == "trim":
                out.append({"key": "job:" + j["id"], "job": j["id"], "from": "part", "name": j["title"],
                            "path": "", "kind": "video" if j.get("clip_fps") else "audio",
                            "thumbnail": j.get("thumbnail") or "", "added": j.get("finished") or j["added"],
                            "duration": j.get("clip_len"), "state": "ready"})
        for j in reversed(snap):
            p = j.get("filepath") or ""
            if j["status"] != "done" or not p or not os.path.isfile(p):
                continue
            sid = sid_for(p)
            if sid in seen or sid in self.hidden:
                continue
            seen.add(sid)
            out.append(self._item(sid, p, "export" if j.get("source") else "download", j))
        with self.lock:
            added = list(self.added)
        for a in reversed(added):
            p = a["path"]
            sid = sid_for(p)
            if sid in seen:
                continue
            seen.add(sid)
            it = self._item(sid, p, "file", None)
            it["added"] = a.get("added")
            it["missing"] = not os.path.isfile(p)
            out.append(it)
        return out

    def _item(self, sid, path, origin, j):
        info = load_info(sid, path) if os.path.isfile(path) else None
        prep = self.preps.get(sid, {})
        state = "ready" if info else prep.get("state", "new")
        kind = ("video" if info.get("fps") else "audio") if info else \
            ((j.get("kind") or "video") if j else ("audio" if re.search(r"\.(mp3|m4a|aac|opus|ogg|oga|flac|wav|aiff?|caf|wma)$", path, re.I) else "video"))
        return {"key": "file:" + sid, "sid": sid, "from": origin, "path": path, "kind": kind,
                "name": os.path.splitext(os.path.basename(path))[0], "job": j["id"] if j else "",
                "thumbnail": (j.get("thumbnail") or "") if j else "", "added": (j.get("finished") if j else None),
                "duration": info.get("len") if info else _job_length(j),
                "state": state, "progress": prep.get("progress", 0), "error": prep.get("error", "")}

    def add(self, path):
        path = os.path.realpath(os.path.expanduser(str(path or "").strip()))
        if not os.path.isfile(path):
            raise RuntimeError("There's no file at %s" % path)
        if not MEDIA_EXT.search(path):
            raise RuntimeError("%s doesn't look like a video or sound file" % os.path.basename(path))
        sid = sid_for(path)
        with self.lock:
            self.hidden.discard(sid)
            if not any(a["path"] == path for a in self.added):
                self.added.append({"path": path, "added": time.time()})
            self._save()
        return {"key": "file:" + sid, "sid": sid}

    def remove(self, sid):
        with self.lock:
            before = len(self.added)
            self.added = [a for a in self.added if sid_for(a["path"]) != sid]
            if len(self.added) == before:
                self.hidden.add(sid)    # a download: off the list, but the file stays where it is
            self._save()
            prep = self.preps.pop(sid, None)
        if prep and prep.get("proc"):
            prep["cancel"] = True
            engine_mod._kill(prep["proc"], 2)
        shutil.rmtree(EDIT_DIR / sid, ignore_errors=True)

    def path_of(self, sid):
        """Only files on the list can be opened, played or exported from."""
        for it in self.items():
            if it.get("sid") == sid:
                return it["path"]
        return None

    # ---- measuring and the playing copy ----------------------------------------
    def open(self, sid):
        path = self.path_of(sid)
        if not path:
            raise RuntimeError("That file isn't on the Edit list any more")
        if not os.path.isfile(path):
            raise RuntimeError("%s isn't there any more (moved or deleted?)" % path)
        info = load_info(sid, path)
        if info:
            os.utime(EDIT_DIR / sid, None)   # in use: keep it from being pruned
            return dict(summary(info, sid), state="ready")
        with self.lock:
            p = self.preps.get(sid)
            if not p or p["state"] in ("error", "ready"):
                p = self.preps[sid] = {"state": "waiting", "progress": 0, "error": ""}
                threading.Thread(target=self._prepare, args=(sid, path, p), daemon=True).start()
        return {"sid": sid, "state": p["state"], "progress": p["progress"], "error": p["error"],
                "name": os.path.basename(path), "path": path}

    def state(self, sid):
        path = self.path_of(sid)
        info = load_info(sid, path) if path else None
        if info:
            return dict(summary(info, sid), state="ready")
        p = self.preps.get(sid) or {"state": "new", "progress": 0, "error": ""}
        return {"sid": sid, "state": p["state"], "progress": p["progress"], "error": p["error"]}

    def _prepare(self, sid, path, p):
        with self.gate:
            if p.get("cancel"):
                return
            p["state"] = "preparing"
            try:
                info = self._measure(sid, path, p)
            except Exception as e:  # noqa: BLE001  shown to the user as is
                info, p["error"] = None, str(e)
            if p.get("cancel"):
                return
            if info:
                p["state"], p["progress"] = "ready", 100
            else:
                p["state"] = "error"
                p["error"] = p["error"] or "NooNoo couldn't read this file"
                engine_mod.notify("Can't edit this file", "%s: %s" % (os.path.basename(path), p["error"]))

    def _measure(self, sid, path, p):
        ffmpeg, ffprobe = str(tools.path("ffmpeg")), str(tools.path("ffprobe"))
        work = EDIT_DIR / sid
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        stamp = _stamp(path)
        try:
            probe = json.loads(subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                                              capture_output=True, text=True, timeout=60).stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            raise RuntimeError("Couldn't read the file: %s" % e)
        streams = probe.get("streams", [])
        video = [x for x in streams if x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")]
        audio = [x for x in streams if x.get("codec_type") == "audio"]
        if not video and not audio:
            raise RuntimeError("There's no picture or sound in it")
        t0 = float(probe.get("format", {}).get("start_time") or 0)
        duration = float(probe.get("format", {}).get("duration") or 0)
        info = {"v": INFO_VERSION, "path": os.path.realpath(path), **stamp, "has_audio": bool(audio), "pts": [],
                "fps": "", "tc0": 0.0}
        if video:
            v = video[0]
            fr = v.get("avg_frame_rate") or "0/0"
            if engine_mod._rate_value(fr) <= 0:
                fr = v.get("r_frame_rate") or "30/1"
            n, _, d = fr.partition("/")
            n, d = int(n), int(d or 1)
            # where the first frame is, so the copy's sound can be lined up with it
            try:
                first = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                                        "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", path],
                                       capture_output=True, text=True, timeout=60).stdout.split()
                pts0 = float(first[0].split(",")[0]) - t0 if first else 0.0
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pts0 = 0.0
            dst = str(work / "preview.mp4")
            # showinfo reports every frame's time as it passes, so measuring costs no extra decoding
            cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "showinfo,setpts=N*%d/%d/TB,scale=-2:'min(540,ih)',format=yuv420p" % (d, n),
                   "-af", engine_mod._sound_at(pts0),
                   "-fps_mode", "passthrough", "-video_track_timescale", str(n),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "12", "-bf", "0",
                   "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", dst]
            pts = self._run(cmd, duration, p, collect=True)
            if not pts:
                raise RuntimeError("There are no frames in it")
            info.update(fps=fr, pts=pts, len=len(pts) / (n / d), width=v.get("width"), height=v.get("height"),
                        codec=v.get("codec_name", ""), tc0=_timecode(probe, n / d))
            wave_from = engine_mod._sound_at(pts0, info["len"]) + ","
        else:
            dst = str(work / "preview.m4a")
            cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", path, "-map", "0:a:0", "-vn", "-c:a", "aac",
                   "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", dst]
            self._run(cmd, duration, p)
            end = engine_mod._audio_end(path)
            info.update(len=(end - t0) if end else duration, codec=audio[0].get("codec_name", ""),
                        tc0=_timecode(probe, 0))
            wave_from = ""
        if p.get("cancel"):
            return None
        if audio:   # a picture of the sound under the timeline, lined up with the frames
            try:
                subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", path, "-lavfi",
                                "[0:a:0]%saformat=channel_layouts=mono,showwavespic=s=4000x%d:colors=0x2f5ba8"
                                % (wave_from, 120 if video else 200),
                                "-frames:v", "1", str(work / "wave.png")], capture_output=True, timeout=900)
                info["wave"] = (work / "wave.png").is_file()
            except (OSError, subprocess.TimeoutExpired):
                pass
        (work / "info.json").write_text(json.dumps(info))
        return info

    def _run(self, cmd, duration, p, collect=False):
        """ffmpeg with progress into p; with collect, the frame times showinfo reports."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True, errors="replace")
        p["proc"] = proc
        pts, tail = [], deque(maxlen=6)

        def read_err():
            for line in proc.stderr:
                if collect and "Parsed_showinfo" in line:
                    m = PTS_RE.search(line)
                    if m:
                        pts.append(float(m.group(1)))
                    continue
                tail.append(line.strip())
        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.isdigit() and duration:
                p["progress"] = round(min(99.0, 100 * int(v) / 1e6 / duration), 1)
        proc.wait()
        t.join(timeout=10)
        p["proc"] = None
        if p.get("cancel"):
            return []
        if proc.returncode != 0:
            errs = [x for x in tail if x]
            raise RuntimeError("Couldn't make a copy to edit with: %s" % (errs[-1] if errs else "ffmpeg failed"))
        return pts

    def media(self, sid, what):
        names = {"video": "preview.mp4", "audio": "preview.m4a", "wave": "wave.png"}
        if what not in names or not re.fullmatch(r"[0-9a-f]{10}", sid or ""):
            return None
        return EDIT_DIR / sid / names[what]
