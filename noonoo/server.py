"""Local HTTP API + static UI. Bound to 127.0.0.1 and guarded by a per-launch token."""
import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import engine as engine_mod
from . import settings as settings_mod
from . import tools

WEB_DIR = Path(__file__).parent / "web"
MEDIA_EXT = {
    "audio": {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav", ".aiff", ".alac", ".wma"},
    "video": {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".3gp", ".mxf"},
}
TOKEN = secrets.token_urlsafe(24)


class App:
    def __init__(self):
        self.engine = engine_mod.Engine()
        self.searcher = engine_mod.Searcher()
        self.tool_log = []
        self.tools_busy = False
        self.window = None  # set by the pywebview launcher

    def install_tools(self, names=None, update=False):
        if self.tools_busy:
            return
        self.tools_busy = True

        def log(msg):
            self.tool_log.append(msg)
            del self.tool_log[:-50]

        def work():
            try:
                if update:
                    tools.update(log)
                else:
                    tools.ensure(log, names)
                log("Done.")
            finally:
                self.tools_busy = False

        threading.Thread(target=work, daemon=True).start()


def _library_roots(s):
    """Default folder plus favourites; nested duplicates dropped so nothing is listed twice."""
    paths = [s["output_dir"], s["current_folder"]] + s["folders"].splitlines()
    roots = []
    for p in paths:
        p = os.path.realpath(os.path.expanduser(p.strip())) if p.strip() else ""
        if p and p not in roots:
            roots.append(p)
    return [r for r in roots if not any(r != o and r.startswith(o + os.sep) for o in roots)]


def _inside(path, roots):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep) for r in roots)


def library(s):
    roots = _library_roots(s)
    files, denied = [], []
    for root in roots:
        try:
            os.listdir(root)
        except PermissionError:  # macOS privacy: Downloads/Documents/Desktop/external drives need permission
            denied.append(root)
            continue
        except OSError:
            continue
        for dirpath, dirnames, names in os.walk(root):
            if dirpath[len(root):].count(os.sep) >= 4 or len(files) > 20000:  # don't crawl a whole disk
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                kind = next((k for k, exts in MEDIA_EXT.items() if ext in exts), None)
                if not kind or n.startswith("."):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append({"path": p, "name": n, "kind": kind, "ext": ext[1:], "size": st.st_size,
                              "mtime": st.st_mtime, "dir": dirpath, "root": root,
                              "folder": os.path.relpath(dirpath, root)})
    files.sort(key=lambda f: -f["mtime"])
    return {"root": roots[0] if roots else "", "roots": roots, "files": files, "denied": denied}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ---- helpers -------------------------------------------------------
        def _send(self, code, body=b"", ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authed(self, q):
            return secrets.compare_digest(self.headers.get("X-Token") or (q.get("t") or [""])[0], TOKEN)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # ---- GET -----------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if u.path == "/":
                if not self._authed(q):
                    return self._send(403, "Forbidden", "text/plain")
                html = (WEB_DIR / "index.html").read_text().replace("__TOKEN__", TOKEN)
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path.startswith("/static/"):
                f = (WEB_DIR / u.path[len("/static/"):]).resolve()
                if not str(f).startswith(str(WEB_DIR.resolve())) or not f.is_file():
                    return self._send(404, {"error": "not found"})
                return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")
            if not self._authed(q):
                return self._send(403, {"error": "forbidden"})
            if u.path == "/api/state":
                snap = app.engine.snapshot()
                snap["tools_busy"] = app.tools_busy
                return self._send(200, snap)
            if u.path == "/api/job":
                return self._send(200, app.engine.job((q.get("id") or [""])[0], with_log=True) or {})
            if u.path == "/api/settings":
                return self._send(200, {"settings": settings_mod.load(), "defaults": settings_mod.DEFAULTS,
                                        "video_formats": {k: v["label"] for k, v in engine_mod.VIDEO_FORMATS.items()},
                                        "edit_formats": [k for k, v in engine_mod.VIDEO_FORMATS.items() if v.get("edit")],
                                        "audio_formats": engine_mod.AUDIO_FORMATS,
                                        "cookie_sites": {k: v[0] for k, v in settings_mod.COOKIE_SITES.items()},
                                        "home": str(Path.home())})
            if u.path == "/api/tools":
                return self._send(200, {"status": tools.status(), "missing": tools.missing(),
                                        "busy": app.tools_busy, "log": app.tool_log[-20:],
                                        "bin": str(tools.BIN_DIR)})
            if u.path == "/api/library":
                return self._send(200, library(settings_mod.load()))
            if u.path == "/media":
                return self._media((q.get("path") or [""])[0])
            return self._send(404, {"error": "not found"})

        do_HEAD = do_GET

        def _allowed(self, path, s):
            return _inside(path, _library_roots(s)) or os.path.realpath(path) in {
                os.path.realpath(p) for p in app.engine.known_files()}

        def _media(self, path):
            s = settings_mod.load()
            if not path or not self._allowed(path, s) or not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            size = os.path.getsize(path)
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else end
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206 if m else 200)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if m:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 16, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # ---- POST ----------------------------------------------------------
        def do_POST(self):
            u = urlparse(self.path)
            if not self._host_ok() or not self._authed({}):
                return self._send(403, {"error": "forbidden"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                return self._send(200, self._route(u.path, body))
            except Exception as e:
                return self._send(500, {"error": str(e)})

        def _route(self, path, b):
            s = settings_mod.load()
            if path == "/api/search":
                if tools.missing():
                    raise RuntimeError("Download engine not installed yet - see Tools > Engine.")
                return app.searcher.search(b.get("id") or "x", b.get("query", ""), b.get("source", "youtube"))
            if path == "/api/search/details":
                return {"details": app.searcher.details(b.get("id") or "x", [u for u in b.get("urls", []) if u][:10],
                                                        b.get("kind", "video"))}
            if path == "/api/search/cancel":
                app.searcher.cancel(b.get("id"))
                return {"ok": True}
            if path == "/api/download":
                return {"ids": app.engine.add(b.get("items", []), b.get("kind", "video"), b.get("quality", ""),
                                              b.get("vformat", ""), b.get("aformat", ""), b.get("folder", ""),
                                              b.get("fps", ""))}
            if path == "/api/job/action":
                ids = b.get("ids") or [b.get("id")]
                for jid in ids:
                    app.engine.action(jid, b.get("action"))
                return {"ok": True}
            if path == "/api/jobs/bulk":
                app.engine.bulk(b.get("action"))
                return {"ok": True}
            if path == "/api/settings":
                return {"settings": settings_mod.save(dict(s, **b.get("settings", {})))}
            if path == "/api/settings/reset":
                return {"settings": settings_mod.save({})}
            if path == "/api/tools/install":
                app.install_tools(update=bool(b.get("update")))
                return {"ok": True}
            if path == "/api/open":
                return self._open(b, s)
            if path == "/api/choose-folder":
                return {"path": choose_folder(b.get("start") or s["output_dir"])}
            if path == "/api/window":
                return window_action(app, b.get("action"), b)
            raise RuntimeError("unknown endpoint")

        def _open(self, b, s):
            what, target = b.get("what"), b.get("path") or ""
            if what == "url":
                if not re.match(r"^https?://", target):
                    raise RuntimeError("not a web link")
                subprocess.Popen(["open", target])
            elif what == "folder":
                d = os.path.expanduser(target or s["current_folder"] or s["output_dir"])
                job_dirs = {os.path.realpath(x.folder) for x in app.engine.jobs.values() if x.folder}
                if target and (d.endswith((".app", ".app/")) or not (
                        _inside(d, _library_roots(s)) or os.path.realpath(d) in job_dirs)):
                    raise RuntimeError("Not one of your download folders")
                os.makedirs(d, exist_ok=True)
                subprocess.Popen(["open", d])
            elif what in ("file", "reveal", "trash"):
                if not self._allowed(target, s) or not os.path.exists(target):
                    raise RuntimeError("File not found (moved or deleted?)")
                if what == "file":
                    subprocess.Popen(["open", target])
                elif what == "reveal":
                    subprocess.Popen(["open", "-R", target])
                else:  # move to Trash via Finder so it can be put back
                    script = 'tell application "Finder" to delete (POSIX file %s as alias)' % json.dumps(target)
                    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30, check=True)
            return {"ok": True}

    return Handler


def choose_folder(start):
    start = os.path.expanduser(start)
    while start and not os.path.isdir(start):
        start = os.path.dirname(start)
    script = ('POSIX path of (choose folder with prompt "Save downloads to:" default location (POSIX file %s))'
              % json.dumps(start or str(Path.home())))
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return out.stdout.strip().rstrip("/") or None


def window_action(app, action, args=None):
    args = args or {}
    w = app.window
    if not w:
        return {"ok": False}
    if action == "minimize":
        w.minimize()
    elif action == "maximize":
        maxed = getattr(app, "_maxed", False)
        try:
            (w.restore if maxed else w.maximize)()
        except AttributeError:
            w.toggle_fullscreen()
        app._maxed = not maxed
    elif action == "resize":
        w.resize(int(args.get("w", 1100)), int(args.get("h", 720)))
    elif action == "close":
        threading.Thread(target=lambda: (time.sleep(0.05), w.destroy()), daemon=True).start()
    return {"ok": True}


def serve(app, port=0):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/?t=%s" % (httpd.server_address[1], TOKEN)
